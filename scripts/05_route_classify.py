import csv, json, subprocess
from collections import defaultdict, Counter
from decimal import Decimal, getcontext
getcontext().prec=60
RPC="https://rpc.mainnet.chain.robinhood.com"
PM="0x8366a39cc670b4001a1121b8f6a443a643e40951"
FIRE="0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG="0x5fc5360d0400a0fd4f2af552add042d716f1d168"
SWAP="0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT="0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
XFER="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
OUT="FIRE_SUSPECT_route_classification.csv"

def rpc(m,p,tries=3):
    body=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}); last=None
    for _ in range(tries):
        r=subprocess.run(["curl","-s",RPC,"-H","Content-Type: application/json","-d",body],
                         capture_output=True,text=True).stdout
        try: d=json.loads(r)
        except Exception as e: last=e; continue
        if "error" in d: last=RuntimeError(d["error"]); continue
        return d["result"]
    raise last

meta={}
def token(a):
    a=a.lower()
    if a in meta: return meta[a]
    try:
        raw=bytes.fromhex(rpc("eth_call",[{"to":a,"data":"0x95d89b41"},"latest"])[2:])
        ln=int.from_bytes(raw[32:64],'big'); sym=raw[64:64+ln].decode(errors="replace")
        dec=int(rpc("eth_call",[{"to":a,"data":"0x313ce567"},"latest"]),16)
    except Exception:
        sym,dec=a[:10],18
    meta[a]=(sym,dec); return meta[a]

keyc={}
def poolkey(pid):
    pid=pid.lower()
    if pid in keyc: return keyc[pid]
    try:
        l=rpc("eth_getLogs",[{"address":PM,"fromBlock":"0x0","toBlock":"latest","topics":[INIT,pid]}])
        c0="0x"+l[0]["topics"][2][26:]; c1="0x"+l[0]["topics"][3][26:]
        fee=int(l[0]["data"][2:66],16)&0xFFFFFF
        keyc[pid]=(c0.lower(),c1.lower(),fee)
    except Exception:
        keyc[pid]=(None,None,None)
    return keyc[pid]

rows=list(csv.DictReader(open("FIRE_USDG_ALL_swaps.csv")))
buys=[r for r in rows if r["direction"]=="USDG_TO_FIRE"]
bytx=defaultdict(list)
for r in buys: bytx[r["transaction_hash"].lower()].append(r)
print(f"{len(buys)} legs in {len(bytx)} txs")

out=[]
for i,(txh,legs) in enumerate(bytx.items(),1):
    tx=rpc("eth_getTransactionByHash",[txh]); rc=rpc("eth_getTransactionReceipt",[txh])
    sender=tx["from"].lower(); value=int(tx["value"],16)
    # every v4 hop in the tx, in order
    hops=[]
    for lg in rc["logs"]:
        if lg["address"].lower()!=PM or not lg["topics"] or lg["topics"][0].lower()!=SWAP: continue
        c0,c1,fee=poolkey(lg["topics"][1])
        s0=token(c0)[0] if c0 and c0!="0x"+"0"*40 else "ETH"
        s1=token(c1)[0] if c1 and c1!="0x"+"0"*40 else "ETH"
        hops.append(f"{s0}/{s1}@{fee}")
    # what did the sender part with? (ERC20 transfers FROM the EOA)
    sent=[]
    for lg in rc["logs"]:
        if not lg["topics"] or lg["topics"][0].lower()!=XFER or len(lg["topics"])<3: continue
        frm="0x"+lg["topics"][1][26:]
        if frm.lower()!=sender: continue
        sym,dec=token(lg["address"])
        sent.append((sym, Decimal(int(lg["data"],16))/Decimal(10**dec)))
    # what did the sender receive?
    recv=[]
    for lg in rc["logs"]:
        if not lg["topics"] or lg["topics"][0].lower()!=XFER or len(lg["topics"])<3: continue
        to="0x"+lg["topics"][2][26:]
        if to.lower()!=sender: continue
        sym,dec=token(lg["address"])
        recv.append((sym, Decimal(int(lg["data"],16))/Decimal(10**dec)))
    if value>0: inp="ETH(native)"
    elif sent: inp="+".join(sorted(set(s for s,_ in sent)))
    else: inp="none_from_eoa"
    out.append({
        "tx_hash":txh, "block":legs[0]["block"], "timestamp":legs[0]["timestamp_utc"],
        "tx_from":sender, "native_value_eth":str(Decimal(value)/Decimal(10**18)),
        "input_token_class":inp,
        "tokens_sent_by_eoa":"|".join(f"{s}:{v}" for s,v in sent) or "",
        "tokens_recv_by_eoa":"|".join(f"{s}:{v}" for s,v in recv) or "",
        "v4_hops":" -> ".join(hops), "n_v4_hops":len(hops),
        "suspect_legs":len(legs),
        "suspect_usdg_in":str(sum(Decimal(l["usdg_in"]) for l in legs)),
        "suspect_fire_out":str(sum(Decimal(l["fire_out"]) for l in legs)),
        "swap_sender":legs[0]["swap_sender"],
    })
    if i%20==0 or i==len(bytx): print(f"[{i:03}/{len(bytx):03}] {inp:18} hops={len(hops)}")

with open(OUT,"w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f,fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
print(f"\nWrote {OUT}\n")
print("INPUT TOKEN CLASS:")
for k,v in Counter(r["input_token_class"] for r in out).most_common(): print(f"  {v:4}  {k}")
print("\nHOP COUNT:")
for k,v in sorted(Counter(r["n_v4_hops"] for r in out).items()): print(f"  {v:4} txs with {k} v4 hop(s)")
print("\nTOP ROUTE SHAPES:")
for k,v in Counter(r["v4_hops"] for r in out).most_common(12): print(f"  {v:4}  {k}")
