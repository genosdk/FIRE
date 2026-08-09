import csv, json, subprocess
from decimal import Decimal, getcontext
from datetime import datetime, timezone
getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM   = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
CANON= "0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
XFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WINDOW_START = 13489232          # first suspect-pool Initialize
SAMPLE = 200
OUT = "FIRE_CANONICAL_hook_delta_audit.csv"

def rpc(m, p, tries=3):
    body = json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p})
    last = None
    for _ in range(tries):
        r = subprocess.run(["curl","-s",RPC,"-H","Content-Type: application/json","-d",body],
                           capture_output=True, text=True).stdout
        try: d = json.loads(r)
        except Exception as e: last = e; continue
        if "error" in d: last = RuntimeError(d["error"]); continue
        return d["result"]
    raise last

def s128(w):
    v = int(w,16) & ((1<<128)-1)
    return v-(1<<128) if v >= 1<<127 else v

# ---- collect canonical swaps in window (chunked; logs are not pruned) ----
latest = int(rpc("eth_blockNumber",[]),16)
logs=[]; CH=250000; s=WINDOW_START
while s <= latest:
    e = min(s+CH-1, latest)
    try:
        logs += rpc("eth_getLogs",[{"address":PM,"fromBlock":hex(s),"toBlock":hex(e),
                                    "topics":[SWAP,CANON]}])
    except Exception as ex:
        print(f"  chunk {s}-{e} failed: {str(ex)[:70]}")
    s = e+1
print(f"canonical swaps in window: {len(logs)}")

# evenly spaced sample across the window
step = max(1, len(logs)//SAMPLE)
sample = logs[::step][:SAMPLE]
print(f"sampling {len(sample)}\n")

# ---- pool key cache, to identify which OTHER pools in a tx involve FIRE ----
keycache={}
def pool_involves_fire(pid):
    pid = pid.lower()
    if pid == CANON: return True
    if pid in keycache: return keycache[pid]
    try:
        l = rpc("eth_getLogs",[{"address":PM,"fromBlock":"0x0","toBlock":"latest","topics":[INIT,pid]}])
        c0 = "0x"+l[0]["topics"][2][26:]; c1 = "0x"+l[0]["topics"][3][26:]
        res = FIRE in (c0.lower(), c1.lower())
    except Exception:
        res = None            # unknown -> treat as possibly-FIRE
    keycache[pid]=res
    return res

blk={}
def ts(bh):
    if bh not in blk:
        blk[bh]=int(rpc("eth_getBlockByNumber",[bh,False])["timestamp"],16)
    return blk[bh]

rows=[]
for i,L in enumerate(sample,1):
    d=L["data"][2:]; w=[d[j:j+64] for j in range(0,len(d),64)]
    a0=s128(w[0]); a1=s128(w[1])          # currency0=ETH, currency1=FIRE
    # v4 Swap amounts are CALLER-CENTRIC: positive = token leaves pool to caller,
    # negative = token enters pool from caller. Verified against ERC-20 Transfer logs.
    direction = "ETH_TO_FIRE" if a1 > 0 else "FIRE_TO_ETH"
    swap_fire = Decimal(abs(a1))/Decimal(10**18)
    txh=L["transactionHash"]; bh=L["blockNumber"]
    r=rpc("eth_getTransactionReceipt",[txh])

    fire_out_pm=0; fire_in_pm=0
    for lg in r["logs"]:
        if lg["address"].lower()!=FIRE or not lg["topics"] or lg["topics"][0].lower()!=XFER:
            continue
        frm="0x"+lg["topics"][1][26:]; to="0x"+lg["topics"][2][26:]
        val=int(lg["data"],16)
        if to.lower()==PM:  fire_in_pm  += val
        if frm.lower()==PM: fire_out_pm += val

    # isolation: how many v4 swaps in this tx, and how many touch FIRE
    pm_swaps=[lg for lg in r["logs"] if lg["address"].lower()==PM and lg["topics"]
              and lg["topics"][0].lower()==SWAP]
    canon_n=sum(1 for lg in pm_swaps if lg["topics"][1].lower()==CANON)
    fire_pools=set()
    for lg in pm_swaps:
        pid=lg["topics"][1].lower()
        if pool_involves_fire(pid) is not False: fire_pools.add(pid)
    clean = (canon_n==1 and len(fire_pools)==1)

    if direction=="ETH_TO_FIRE":
        receipt_fire = Decimal(fire_out_pm)/Decimal(10**18)
    else:
        receipt_fire = Decimal(fire_in_pm)/Decimal(10**18)

    diff = receipt_fire - swap_fire
    pct  = (diff/swap_fire*100) if swap_fire>0 else Decimal(0)
    detected = "yes" if (clean and swap_fire>0 and abs(pct)>Decimal("0.0001")) else \
               ("no" if clean else "n/a_multi_pool")

    rows.append({
        "block":int(bh,16),
        "timestamp":datetime.fromtimestamp(ts(bh),tz=timezone.utc).isoformat(),
        "tx_hash":txh,
        "direction":direction,
        "swap_event_fire":f"{swap_fire:.18f}",
        "fire_out_of_poolmanager":f"{Decimal(fire_out_pm)/Decimal(10**18):.18f}",
        "fire_into_poolmanager":f"{Decimal(fire_in_pm)/Decimal(10**18):.18f}",
        "receipt_fire_compared":f"{receipt_fire:.18f}",
        "difference_fire":f"{diff:.18f}",
        "difference_pct":f"{pct:.8f}",
        "v4_swaps_in_tx":len(pm_swaps),
        "canonical_swaps_in_tx":canon_n,
        "fire_pools_in_tx":len(fire_pools),
        "cleanly_isolated":clean,
        "hook_delta_detected":detected,
    })
    if i%20==0 or i==len(sample):
        print(f"[{i:03}/{len(sample):03}] {rows[-1]['timestamp'][:19]} {direction:12} "
              f"swap={swap_fire:,.4f} receipt={receipt_fire:,.4f} diff={pct:+.6f}% "
              f"{'CLEAN' if clean else 'multi'}")

with open(OUT,"w",newline="",encoding="utf-8") as f:
    wr=csv.DictWriter(f,fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)
print(f"\nWrote {OUT}")

clean=[r for r in rows if r["cleanly_isolated"]]
multi=[r for r in rows if not r["cleanly_isolated"]]
print("="*72); print("HOOK DELTA AUDIT SUMMARY"); print("="*72)
print(f"sampled:            {len(rows)}")
print(f"cleanly isolated:   {len(clean)}")
print(f"multi-pool (excl.): {len(multi)}")
if clean:
    ds=sorted(Decimal(r["difference_pct"]) for r in clean)
    hits=[r for r in clean if r["hook_delta_detected"]=="yes"]
    print(f"\nhook delta detected: {len(hits)}/{len(clean)} ({len(hits)/len(clean):.1%})")
    print(f"  median diff: {ds[len(ds)//2]:+.8f}%")
    print(f"  min  diff:   {min(ds):+.8f}%")
    print(f"  max  diff:   {max(ds):+.8f}%")
    exact=sum(1 for r in clean if Decimal(r["difference_fire"])==0)
    print(f"  exact matches (0 wei diff): {exact}/{len(clean)}")
    for lab in ("ETH_TO_FIRE","FIRE_TO_ETH"):
        sub=[r for r in clean if r["direction"]==lab]
        if sub:
            sd=sorted(Decimal(r["difference_pct"]) for r in sub)
            print(f"  {lab}: n={len(sub)} median={sd[len(sd)//2]:+.8f}% "
                  f"max|diff|={max(abs(x) for x in sd):.8f}%")
