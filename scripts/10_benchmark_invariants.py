import csv, json, subprocess
from decimal import Decimal, getcontext
from eth_abi import decode as abidecode
from eth_utils import keccak
getcontext().prec=60
RPC="https://rpc.mainnet.chain.robinhood.com"
PM="0x8366a39cc670b4001a1121b8f6a443a643e40951"
CANON="0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
V3="0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
WETH="0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG="0x5fc5360d0400a0fd4f2af552add042d716f1d168"
DEPOSITORY="0x4cd00e387622c35bddb9b4c962c136462338bc31"
V4SWAP="0x"+keccak(text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
V3SWAP="0x"+keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
ERC20DEP="0x"+keccak(text="RelayErc20Deposit(address,address,uint256,bytes32)").hex()

def rpc(m,p,tries=3):
    b=json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}); last=None
    for _ in range(tries):
        try:
            r=json.loads(subprocess.run(["curl","-sS","--max-time","40",RPC,
                "-H","Content-Type: application/json","-d",b],capture_output=True,text=True).stdout)
            if "error" in r: last=RuntimeError(r["error"]); continue
            return r["result"]
        except Exception as e: last=e
    raise last
def s128(w):
    v=int(w,16)&((1<<128)-1); return v-(1<<128) if v>=1<<127 else v
def s256(w):
    v=int(w,16); return v-(1<<256) if v>=1<<255 else v

bench=list(csv.DictReader(open("FIRE_RELAY_EXECUTION_BENCHMARK.csv")))
orders=list(csv.DictReader(open("FIRE_RELAY_ORDERS.csv")))

print("="*78); print("INVARIANT 1  actual_relay_deposit_usdg == on-chain RelayErc20Deposit (FIRE source)"); print("="*78)
ok=bad=0
for r in bench:
    rc=rpc("eth_getTransactionReceipt",[r["origin_tx"]])
    src=r["fire_source"].lower(); found=None; total_deps=0
    for lg in rc["logs"]:
        if lg["address"].lower()!=DEPOSITORY or not lg["topics"]: continue
        if lg["topics"][0].lower()!=ERC20DEP.lower(): continue
        total_deps+=1
        dep,tokn,amt,oid=abidecode(["address","address","uint256","bytes32"],bytes.fromhex(lg["data"][2:]))
        if dep.lower()==src:
            found=(Decimal(amt)/Decimal(10**6), tokn.lower(), "0x"+oid.hex())
    if found is None:
        print(f"  FAIL {r['origin_tx'][:14]}… no deposit credited to FIRE source"); bad+=1; continue
    match = found[0]==Decimal(r["actual_relay_deposit_usdg"]) and found[1]==USDG
    ok+= match; bad+= (not match)
    if not match:
        print(f"  MISMATCH {r['origin_tx'][:14]}… csv={r['actual_relay_deposit_usdg']} chain={found[0]}")
    elif total_deps>1:
        print(f"  ok {r['origin_tx'][:14]}… ({total_deps} deposits in tx, correctly used the FIRE-source one: {found[0]} USDG)")
print(f"  -> {ok}/{len(bench)} exact matches, {bad} problems\n")

print("="*78); print("INVARIANT 2  canonical selections: FIRE<0 and native ETH>0 (v4 caller-centric)"); print("="*78)
ctx=set()
for r in bench: ctx.update(t for t in r["canonical_match_txs"].split("|") if t)
bad2=0; n2=0
for t in sorted(ctx):
    rc=rpc("eth_getTransactionReceipt",[t])
    for lg in rc["logs"]:
        if lg["address"].lower()!=PM or not lg["topics"]: continue
        if lg["topics"][0].lower()!=V4SWAP.lower(): continue
        if lg["topics"][1].lower()!=CANON: continue
        d=lg["data"][2:]; w=[d[i:i+64] for i in range(0,len(d),64)]
        a0,a1=s128(w[0]),s128(w[1])   # a0 = native ETH, a1 = FIRE
        n2+=1
        if not (a1<0 and a0>0):
            bad2+=1; print(f"  VIOLATION {t[:14]}… ETH={a0} FIRE={a1}")
print(f"  -> {n2} canonical swaps checked across {len(ctx)} txs, {bad2} violations\n")

print("="*78); print("INVARIANT 3  bridge selections: WETH>0 and USDG<0 (v3 pool-centric)"); print("="*78)
btx=set()
for r in bench: btx.update(t for t in r["bridge_match_txs"].split("|") if t)
t0=("0x"+rpc("eth_call",[{"to":V3,"data":"0x0dfe1681"},"latest"])[-40:]).lower()
t1=("0x"+rpc("eth_call",[{"to":V3,"data":"0xd21220a7"},"latest"])[-40:]).lower()
bad3=0; n3=0
for t in sorted(btx):
    rc=rpc("eth_getTransactionReceipt",[t])
    for lg in rc["logs"]:
        if lg["address"].lower()!=V3 or not lg["topics"]: continue
        if lg["topics"][0].lower()!=V3SWAP.lower(): continue
        d=lg["data"][2:]; w=[d[i:i+64] for i in range(0,len(d),64)]
        amts={t0:s256(w[0]), t1:s256(w[1])}
        n3+=1
        if not (amts[WETH]>0 and amts[USDG]<0):
            bad3+=1; print(f"  VIOLATION {t[:14]}… WETH={amts[WETH]} USDG={amts[USDG]}")
print(f"  -> {n3} v3 swaps checked across {len(btx)} txs, {bad3} violations\n")

print("="*78); print("INVARIANT 4  manual recompute of synthetic_benchmark_usdg"); print("="*78)
for r in bench[:3]:
    f=Decimal(r["fire_sold"]); c=Decimal(r["canonical_eth_per_fire"]); b=Decimal(r["bridge_usdg_per_eth"])
    eth=f*c; man=eth*b
    print(f"  {r['origin_tx'][:14]}…")
    print(f"     FIRE {f} x {c} = {eth}")
    print(f"     stored synthetic_eth_output   = {r['synthetic_eth_output']}   {'MATCH' if eth==Decimal(r['synthetic_eth_output']) else 'DIFFER'}")
    print(f"     {eth} x {b} = {man}")
    print(f"     stored synthetic_benchmark_usdg = {r['synthetic_benchmark_usdg']}   {'MATCH' if man==Decimal(r['synthetic_benchmark_usdg']) else 'DIFFER'}")
    d=Decimal(r["actual_relay_deposit_usdg"])-man
    pct=d/man*100
    print(f"     diff {d} -> {pct:+.6f}%  stored {r['actual_vs_benchmark_pct']}  "
          f"{'MATCH' if pct==Decimal(r['actual_vs_benchmark_pct']) else 'DIFFER'}")
