"""Pass 2A step 1: chain-wide enumeration of every Uniswap v4 pool on Robinhood Chain.

The FIRE cohort's 3,400 pools turned out to be one participant's share of a much
larger ecosystem -- LICKLET showed ten distinct seed wallets racing on a single
token, none of them cohort members. So the population is every Initialize event,
with the cohort kept as a labelled subset.

Stores a compact record per pool and caches it, because this is ~258k events.
"""
import csv, json, os, subprocess, time
from collections import Counter, defaultdict

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
CHUNK = 250_000
CACHE = "data/analysis/_chainwide_pools.jsonl"
OUT = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"

NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
BASE = {NATIVE, USDG, WETH}


def rpc(m, p, tries=5):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "90", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.5 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.5 * (a + 1))
    raise last


def get_logs_adaptive(lo, hi, depth=0):
    """eth_getLogs, halving the range when the node's 10,000-log cap is hit.
    Pool creation density rises sharply in recent blocks, so a fixed chunk size
    that works early in the chain fails near the head."""
    try:
        return rpc("eth_getLogs", [{"address": PM, "fromBlock": hex(lo),
                                    "toBlock": hex(hi), "topics": [INIT]}], tries=2)
    except Exception as e:
        if "exceeds limit" not in str(e) or lo >= hi or depth > 12:
            raise
        mid = (lo + hi) // 2
        return get_logs_adaptive(lo, mid, depth + 1) + get_logs_adaptive(mid + 1, hi, depth + 1)


def i24(w):
    v = int(w, 16) & 0xFFFFFF
    return v - 0x1000000 if v & 0x800000 else v


latest = int(rpc("eth_blockNumber", []), 16)
done_to = -1
if os.path.exists(CACHE):
    with open(CACHE) as f:
        for line in f:
            try:
                done_to = max(done_to, json.loads(line)["b"])
            except Exception:
                pass
    print(f"cache present, highest cached block {done_to:,}")

start = 0 if done_to < 0 else (done_to // CHUNK) * CHUNK
n_new = 0
with open(CACHE, "a") as out:
    s = start
    while s <= latest:
        e = min(s + CHUNK - 1, latest)
        lg = get_logs_adaptive(s, e)
        for L in lg:
            b = int(L["blockNumber"], 16)
            if b <= done_to:
                continue
            d = L["data"][2:]
            w = [d[i:i + 64] for i in range(0, len(d), 64)]
            out.write(json.dumps({
                "p": L["topics"][1].lower(),
                "c0": "0x" + L["topics"][2][26:], "c1": "0x" + L["topics"][3][26:],
                "f": int(w[0], 16) & 0xFFFFFF, "ts": i24(w[1]), "h": "0x" + w[2][24:],
                "sp": int(w[3], 16), "tk": i24(w[4]),
                "b": b, "tx": L["transactionHash"]}) + "\n")
            n_new += 1
        if (s // CHUNK) % 20 == 0 or e >= latest:
            print(f"  {e:,}/{latest:,}  (+{n_new} new)")
        s = e + 1
print(f"\nnew records: {n_new}")

pools = {}
with open(CACHE) as f:
    for line in f:
        try:
            r = json.loads(line)
            pools[r["p"]] = r
        except Exception:
            pass
print(f"total distinct pools: {len(pools):,}")

# FIRE cohort label
cohort = set()
if os.path.exists("data/analysis/CENSUS_POOLS.csv"):
    cohort = {r["pool_id"] for r in csv.DictReader(open("data/analysis/CENSUS_POOLS.csv"))}

DYN = 0x800000
def band(f):
    if f == DYN: return "dynamic"
    if f < 10000: return "<1%"
    if f < 100000: return "1-10%"
    if f < 400000: return "10-40%"
    if f < 700000: return "40-70%"
    if f < 900000: return "70-90%"
    return "90%+"


rows = []
for p in pools.values():
    c0, c1 = p["c0"].lower(), p["c1"].lower()
    nonbase = [c for c in (c0, c1) if c not in BASE]
    rows.append({"pool_id": p["p"], "token0": c0, "token1": c1,
                 "fee": p["f"], "fee_band": band(p["f"]), "tick_spacing": p["ts"],
                 "hooks": p["h"], "hooked": p["h"].lower() != NATIVE,
                 "ts_equals_fee_over_100": p["ts"] == p["f"] // 100 if p["f"] != DYN else False,
                 "init_block": p["b"], "init_tx": p["tx"],
                 "init_tick": p["tk"],
                 "non_base_token": nonbase[0] if len(nonbase) == 1 else "",
                 "pair_kind": ("token/base" if len(nonbase) == 1 else
                               "base/base" if not nonbase else "token/token"),
                 "fire_cohort": p["p"] in cohort})
rows.sort(key=lambda r: r["init_block"])
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print(f"Wrote {OUT}")

print("\n" + "=" * 74)
print("CHAIN-WIDE v4 POOL CENSUS")
print("=" * 74)
print(f"total pools:            {len(rows):,}")
toks = {r["non_base_token"] for r in rows if r["non_base_token"]}
print(f"distinct non-base tokens:{len(toks):,}")
print(f"FIRE-cohort pools:      {sum(1 for r in rows if r['fire_cohort']):,} "
      f"({sum(1 for r in rows if r['fire_cohort'])/len(rows)*100:.2f}% of chain)")

print("\nFEE BAND (all pools)")
c = Counter(r["fee_band"] for r in rows)
order = ["<1%", "1-10%", "10-40%", "40-70%", "70-90%", "90%+", "dynamic"]
for b in order:
    if c.get(b):
        print(f"  {b:8} {c[b]:>8,} ({c[b]/len(rows)*100:>5.1f}%)")
ext = sum(v for k, v in c.items() if k in ("10-40%", "40-70%", "70-90%", "90%+"))
print(f"  extreme (>=10%): {ext:,} ({ext/len(rows)*100:.1f}%)")

print("\nFEE BAND -- FIRE cohort vs rest of chain")
print(f"  {'band':8} {'cohort':>8} {'rest':>10} {'cohort share':>13}")
for b in order:
    ch = sum(1 for r in rows if r["fire_cohort"] and r["fee_band"] == b)
    rest = c.get(b, 0) - ch
    if c.get(b):
        print(f"  {b:8} {ch:>8,} {rest:>10,} {ch/c[b]*100:>12.1f}%")

print("\nPAIR KIND")
for k, v in Counter(r["pair_kind"] for r in rows).most_common():
    print(f"  {k:12} {v:>8,} ({v/len(rows)*100:>5.1f}%)")
print("\nHOOKS")
hk = Counter("hooked" if r["hooked"] else "hookless" for r in rows)
for k, v in hk.most_common():
    print(f"  {k:10} {v:>8,} ({v/len(rows)*100:>5.1f}%)")
print("\ntickSpacing == fee/100 (candidate bot-family signature)")
sig = sum(1 for r in rows if r["ts_equals_fee_over_100"])
sigext = sum(1 for r in rows if r["ts_equals_fee_over_100"] and r["fee"] >= 100000)
print(f"  all pools:      {sig:,} ({sig/len(rows)*100:.1f}%)")
print(f"  extreme pools:  {sigext:,} of {ext:,} ({sigext/ext*100:.1f}%)")

print("\nTOKEN OPPORTUNITIES (non-base token with >=1 extreme pool)")
byt = defaultdict(list)
for r in rows:
    if r["non_base_token"] and r["fee"] != DYN and r["fee"] >= 100000:
        byt[r["non_base_token"]].append(r)
print(f"  tokens with >=1 extreme pool: {len(byt):,}")
mult = Counter(len(v) for v in byt.values())
print(f"  with 1 extreme pool:  {mult.get(1,0):,}")
print(f"  with 2-4:             {sum(v for k,v in mult.items() if 2<=k<=4):,}")
print(f"  with 5-9:             {sum(v for k,v in mult.items() if 5<=k<=9):,}")
print(f"  with 10+:             {sum(v for k,v in mult.items() if k>=10):,}")
print(f"  max extreme pools on one token: {max(mult)}")
