import csv, json, subprocess
from collections import defaultdict, Counter
from decimal import Decimal, getcontext
getcontext().prec=60

RPC="https://rpc.mainnet.chain.robinhood.com"
BS ="https://robinhoodchain.blockscout.com"
PM ="0x8366a39cc670b4001a1121b8f6a443a643e40951"
CANON="0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
P50="0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43"
P40="0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636"
SWAP="0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT="0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
XFER="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEP ="0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"  # WETH Deposit
WDR ="0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"  # WETH Withdrawal
NATIVE="0x0000000000000000000000000000000000000000"
OUT="FIRE_CYCLE_profitability.csv"

def rpc(m,p,tries=3):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}); last=None
    for _ in range(tries):
        r=subprocess.run(["curl","-s","-m","40",RPC,"-H","Content-Type: application/json","-d",body],
                         capture_output=True,text=True).stdout
        try: d=json.loads(r)
        except Exception as e: last=e; continue
        if "error" in d: last=RuntimeError(d["error"]); continue
        return d["result"]
    raise last

def trace(txh,tries=3):
    for _ in range(tries):
        r=subprocess.run(["curl","-s","-m","60",f"{BS}/api/v2/transactions/{txh}/raw-trace"],
                         capture_output=True,text=True).stdout
        try:
            d=json.loads(r)
            if isinstance(d,dict) and ("calls" in d or "from" in d): return d
        except Exception: pass
    return None

def s128(w):
    v=int(w,16)&((1<<128)-1); return v-(1<<128) if v>=1<<127 else v

tokmeta={}
def tok(a):
    a=(a or "").lower()
    if a==NATIVE: return ("ETH",18)
    if a in tokmeta: return tokmeta[a]
    try:
        raw=bytes.fromhex(rpc("eth_call",[{"to":a,"data":"0x95d89b41"},"latest"])[2:])
        ln=int.from_bytes(raw[32:64],'big'); sym=raw[64:64+ln].decode(errors="replace")
        dec=int(rpc("eth_call",[{"to":a,"data":"0x313ce567"},"latest"]),16)
    except Exception:
        sym,dec=a[:10],18
    tokmeta[a]=(sym,dec); return tokmeta[a]

keyc={}
def poolkey(pid):
    pid=pid.lower()
    if pid in keyc: return keyc[pid]
    try:
        l=rpc("eth_getLogs",[{"address":PM,"fromBlock":"0x0","toBlock":"latest","topics":[INIT,pid]}])
        c0=("0x"+l[0]["topics"][2][26:]).lower(); c1=("0x"+l[0]["topics"][3][26:]).lower()
        fee=int(l[0]["data"][2:66],16)&0xFFFFFF
        keyc[pid]=(c0,c1,fee)
    except Exception: keyc[pid]=(None,None,None)
    return keyc[pid]

def walk(node, sink):
    """collect native value transfers from the call tree"""
    if not isinstance(node,dict): return
    v=node.get("value")
    if v and int(v,16)>0 and node.get("type") not in ("STATICCALL","DELEGATECALL"):
        sink.append((node.get("from","").lower(), (node.get("to") or "").lower(), int(v,16)))
    for c in node.get("calls",[]) or []: walk(c,sink)

rows=list(csv.DictReader(open("../route/FIRE_SUSPECT_route_classification.csv")))
print(f"analysing {len(rows)} suspect transactions\n")
out=[]
for i,R in enumerate(rows,1):
    txh=R["tx_hash"]
    rc=rpc("eth_getTransactionReceipt",[txh]); tx=rpc("eth_getTransactionByHash",[txh])
    tr=trace(txh)

    # ---- v4 hops, caller-centric deltas ----
    hops=[]; net=defaultdict(Decimal); shape=[]
    pools_hit=set()
    for lg in rc["logs"]:
        if lg["address"].lower()!=PM or not lg["topics"] or lg["topics"][0].lower()!=SWAP: continue
        pid=lg["topics"][1].lower(); c0,c1,fee=poolkey(pid)
        d=lg["data"][2:]; w=[d[j:j+64] for j in range(0,len(d),64)]
        a0=s128(w[0]); a1=s128(w[1])
        s0,d0=tok(c0); s1,d1=tok(c1)
        net[s0]+=Decimal(a0)/Decimal(10**d0)
        net[s1]+=Decimal(a1)/Decimal(10**d1)
        pools_hit.add(pid); shape.append(f"{s0}/{s1}@{fee}")
        hops.append((pid,s0,s1,a0,a1,d0,d1))

    # ---- full token ledger (ERC20 + native + WETH wrap) ----
    ledger=defaultdict(lambda: defaultdict(Decimal)); wrapped=False
    for lg in rc["logs"]:
        t0=lg["topics"][0].lower() if lg["topics"] else ""
        a=lg["address"].lower(); sym,dec=tok(a)
        if t0==XFER and len(lg["topics"])>=3:
            f="0x"+lg["topics"][1][26:]; t="0x"+lg["topics"][2][26:]
            v=Decimal(int(lg["data"],16))/Decimal(10**dec)
            ledger[f.lower()][sym]-=v; ledger[t.lower()][sym]+=v
        elif t0 in (DEP,WDR):
            wrapped=True
    natives=[]
    if tr: walk(tr,natives)
    for f,t,v in natives:
        val=Decimal(v)/Decimal(10**18)
        if f: ledger[f]["ETH"]-=val
        if t: ledger[t]["ETH"]+=val

    # ---- residual vector from v4 net (dust-tolerant) ----
    resid={k:v for k,v in net.items() if abs(v)>Decimal("1e-12")}
    signif={k:v for k,v in resid.items() if abs(v)>Decimal("1e-9")}
    pos=[k for k,v in signif.items() if v>0]; neg=[k for k,v in signif.items() if v<0]

    cls="MULTI_ASSET_UNRESOLVED"; asset=""; cin=cout=gp=Decimal(0)
    ETHY={"ETH","WETH"}
    if not hops:
        cls="INSUFFICIENT_ACCOUNTING"
    elif len(signif)==1 and pos:
        cls="CLOSED_CYCLE"; asset=pos[0]; gp=signif[asset]
        cin=(sum((-Decimal(h[3])/Decimal(10**h[5]) for h in hops if h[1]==asset and h[3]<0), Decimal(0))
             + sum((-Decimal(h[4])/Decimal(10**h[6]) for h in hops if h[2]==asset and h[4]<0), Decimal(0)))
        cout=cin+gp
    elif len(signif)==2 and set(signif)<=ETHY and pos and neg:
        tot=sum(signif.values())
        if wrapped:
            cls="CLOSED_CYCLE_ETH_WETH"; asset="ETH-equivalent"; gp=tot
            cin=(sum((-Decimal(h[3])/Decimal(10**h[5]) for h in hops if h[1] in ETHY and h[3]<0), Decimal(0))
                 + sum((-Decimal(h[4])/Decimal(10**h[6]) for h in hops if h[2] in ETHY and h[4]<0), Decimal(0)))
            cout=cin+gp
        else:
            cls="MULTI_ASSET_UNRESOLVED"
    elif len(signif)==2 and len(pos)==1 and len(neg)==1:
        cls="ONE_WAY_ROUTE"; asset=f"{neg[0]}->{pos[0]}"
        cin=-signif[neg[0]]; cout=signif[pos[0]]
    elif len(signif)==0:
        cls="CLOSED_CYCLE"; asset="none"; gp=Decimal(0)

    gas_used=int(rc["gasUsed"],16); gp_price=int(rc.get("effectiveGasPrice") or tx.get("gasPrice") or "0x0",16)
    gas_eth=Decimal(gas_used*gp_price)/Decimal(10**18)
    gas_payer=tx["from"].lower()
    # profit recipient: address holding the largest positive residual in the cycle asset
    prec=""; pconf="low"
    if cls.startswith("CLOSED_CYCLE") and asset not in ("","none"):
        cand=[(ad,vs.get(asset.replace("-equivalent",""),Decimal(0))) for ad,vs in ledger.items()]
        cand=[(a,v) for a,v in cand if v>0 and a not in (PM,)]
        if cand:
            cand.sort(key=lambda x:-x[1]); prec=cand[0][0]
            pconf="high" if (prec==gas_payer) else "medium"

    fees=[poolkey(p)[2] for p in pools_hit]
    susp=[l for l in rc["logs"] if l["address"].lower()==PM and l["topics"] and
          l["topics"][0].lower()==SWAP and l["topics"][1].lower() in (P50,P40)]
    pfa=""
    for l in susp:
        d=l["data"][2:]; w=[d[j:j+64] for j in range(0,len(d),64)]
        fr=int(w[5],16)&0xFFFFFF
        pfa="yes" if fr in (500500,400600) else "no"
    out.append({
        "tx_hash":txh,"timestamp":R["timestamp"],"block":R["block"],
        "route_shape":" -> ".join(shape),"n_v4_hops":len(hops),
        "suspect_pool_50_touched":P50 in pools_hit,"suspect_pool_40_touched":P40 in pools_hit,
        "canonical_pool_touched":CANON in pools_hit,
        "classification":cls,"cycle_asset":asset,
        "cycle_input":f"{cin:.18f}","cycle_output":f"{cout:.18f}",
        "gross_profit":f"{gp:.18f}",
        "gross_profit_pct":(f"{(gp/cin*100):.6f}" if cin>0 else ""),
        "gas_payer":gas_payer,"gas_cost_eth":f"{gas_eth:.18f}",
        "net_profit_after_gas_if_attributable":(f"{(gp-gas_eth):.18f}" if cls=="CLOSED_CYCLE_ETH_WETH" or (cls=="CLOSED_CYCLE" and asset in ("ETH","WETH")) else ""),
        "profit_recipient":prec,"profit_recipient_confidence":pconf,
        "all_residual_tokens":"|".join(f"{k}:{v:+.10f}" for k,v in sorted(signif.items())),
        "accounting_balanced":(len(signif)<=1),
        "trace_ok":bool(tr),"weth_wrap_events":wrapped,
        "swap_sender":R["swap_sender"],"tx_from":R["tx_from"],
        "protocol_fee_active":pfa,
    })
    if i%15==0 or i==len(rows):
        print(f"[{i:03}/{len(rows):03}] {cls:26} asset={asset:16} gp={gp:+.8f}")

with open(OUT,"w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
print(f"\nWrote {OUT}\n")
print("CLASSIFICATION:")
for k,v in Counter(r["classification"] for r in out).most_common(): print(f"  {v:4}  {k}")
print("\ntrace fetch ok:",sum(1 for r in out if r["trace_ok"]),"/",len(out))
cyc=[r for r in out if r["classification"].startswith("CLOSED_CYCLE") and r["gross_profit_pct"]]
print(f"\nELIGIBLE CLOSED CYCLES: {len(cyc)}")
if cyc:
    ps=sorted(Decimal(r["gross_profit_pct"]) for r in cyc)
    prof=[x for x in ps if x>Decimal("0.001")]; loss=[x for x in ps if x<Decimal("-0.001")]
    be=len(ps)-len(prof)-len(loss)
    def q(p): return ps[min(len(ps)-1,int(len(ps)*p))]
    print(f"  profitable gross: {len(prof)} ({len(prof)/len(ps):.1%})")
    print(f"  break-even:       {be}")
    print(f"  loss-making:      {len(loss)} ({len(loss)/len(ps):.1%})")
    print(f"  median return:    {q(0.5):+.4f}%")
    print(f"  p10/p25/p75/p90:  {q(0.1):+.4f}% / {q(0.25):+.4f}% / {q(0.75):+.4f}% / {q(0.9):+.4f}%")
    for lab,key in (("50% pool","suspect_pool_50_touched"),("40% pool","suspect_pool_40_touched"),
                    ("canonical present","canonical_pool_touched")):
        sub=sorted(Decimal(r["gross_profit_pct"]) for r in cyc if str(r[key])=="True")
        if sub: print(f"  {lab:18} n={len(sub):3} median={sub[len(sub)//2]:+.4f}%")
    for pf in ("no","yes"):
        sub=sorted(Decimal(r["gross_profit_pct"]) for r in cyc if r["protocol_fee_active"]==pf)
        if sub: print(f"  protocol_fee={pf:3}      n={len(sub):3} median={sub[len(sub)//2]:+.4f}%")
