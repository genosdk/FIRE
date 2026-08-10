"""Post-processing for the lifecycle validation run.

Re-derives outcomes from the cached raw logs, applying corrections that do not
justify paying the retrieval cost again:

  * "aggregator" -> ROUTER_LIKE_FLOW. A sender appearing across many pools is
    shared infrastructure, which could equally be an arbitrage or market-making
    bot. Known 0x / Relay / Uniswap addresses are attributed separately.
  * prevalence sensitivity at thresholds 3, 5 and 10.
  * exact deployment ordering by (block, transaction_index, log_index) -- on a
    ~100 ms chain same-block races are plausible. Initialize logs are re-fetched
    per sampled token only, which is cheap.
  * extreme_rank among competing extreme pools AND all_pool_rank among every
    pool initialized for that token.
  * UNSEEDED vs SEED_LOOKUP_FAILED: an initialized pool with no liquidity is
    real data; only retrieval failure is a completeness problem. Since
    logs_adaptive raises on failure, a cached pool with no ModifyLiquidity is
    genuinely unseeded.
  * volume winner by routed swap COUNT, not raw token amounts, because
    competing pools may use different base assets.
"""
import csv, json, os, subprocess, time
from collections import Counter, defaultdict

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
CACHE = "data/analysis/_lifecycle_val_cache.json"
POOLS = "data/analysis/LIFECYCLE_VALIDATION_POOLS.csv"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
ORDER_CACHE = "data/analysis/_init_order_cache.json"
OUT = "data/analysis/LIFECYCLE_VALIDATION_REFINED.csv"

LICKLET = "0x23192efc86d38f8b9de39af603b73521ec346bcc"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
KNOWN_INFRA = {
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap UniversalRouter",
    "0x1d4b86491ec211257cbedd77a4380a7494624eff": "RobinHoodSettler (0x)",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7": "RobinHoodSettler (0x)",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f": "RobinHoodSettler (0x)",
    "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef": "Relay solver",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be": "RelayApprovalProxyV3",
}


def rpc(m, p, tries=4):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "60", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.4 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.4 * (a + 1))
    raise last


cache = json.load(open(CACHE))
base = list(csv.DictReader(open(POOLS)))
print(f"pools in validation output: {len(base)}, cached raw records: {len(cache)}")

# ---- exact initialization ordering, per sampled token --------------------
tokens = sorted({r["token"] for r in base})
order = json.load(open(ORDER_CACHE)) if os.path.exists(ORDER_CACHE) else {}
for i, t in enumerate(tokens, 1):
    if t in order:
        continue
    tt = "0x" + "00" * 12 + t[2:]
    got = []
    for topics in ([INIT, None, tt, None], [INIT, None, None, tt]):
        got += rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0",
                                    "toBlock": "latest", "topics": topics}])
    order[t] = [{"p": L["topics"][1].lower(), "b": int(L["blockNumber"], 16),
                 "ti": int(L["transactionIndex"], 16), "li": int(L["logIndex"], 16)}
                for L in got]
    if i % 10 == 0:
        json.dump(order, open(ORDER_CACHE, "w"))
        print(f"  ordering {i}/{len(tokens)}")
json.dump(order, open(ORDER_CACHE, "w"))

pos = {}
for t, lst in order.items():
    allp = sorted(lst, key=lambda x: (x["b"], x["ti"], x["li"]))
    for i, x in enumerate(allp, 1):
        pos[x["p"]] = {"all_rank": i, "n_all": len(allp), "key": (x["b"], x["ti"], x["li"])}

by_tok = defaultdict(list)
for r in base:
    by_tok[r["token"]].append(r)
for t, rs in by_tok.items():
    rs.sort(key=lambda r: pos.get(r["pool_id"], {}).get("key", (int(r["init_block"]), 0, 0)))
    for i, r in enumerate(rs, 1):
        r["extreme_rank"] = i
        p = pos.get(r["pool_id"])
        r["all_pool_rank"] = p["all_rank"] if p else ""
        r["n_all_pools_for_token"] = p["n_all"] if p else ""

# ---- router-like prevalence sensitivity ---------------------------------
sender_pools = defaultdict(set)
for r in base:
    for s in cache[r["pool_id"]]["swaps"]:
        sender_pools[s["s"]].add(r["pool_id"])

print(f"\ndistinct swap senders: {len(sender_pools)}")
for th in (3, 5, 10):
    rl = {s for s, ps in sender_pools.items() if len(ps) >= th}
    known = {s for s in rl if s in KNOWN_INFRA}
    print(f"  threshold {th:>2}: {len(rl):>4} router-like senders ({len(known)} known infra)")

known_seen = {s: KNOWN_INFRA[s] for s in sender_pools if s in KNOWN_INFRA}
print("\nknown infrastructure observed in sample:")
for s, n in sorted(known_seen.items(), key=lambda x: -len(sender_pools[x[0]])):
    print(f"  {s}  {n:26} in {len(sender_pools[s])} pools")

rows = []
for r in base:
    rec = cache[r["pool_id"]]
    sw = sorted(rec["swaps"], key=lambda x: x["b"])
    mods = sorted(rec["mods"], key=lambda x: x["b"])
    e = dict(r)
    e["seed_status"] = "SEEDED" if mods else "UNSEEDED"   # retrieval failures raise, so never silent
    e["n_swaps"] = len(sw)
    e["known_infra_swaps"] = sum(1 for s in sw if s["s"] in KNOWN_INFRA)
    for th in (3, 5, 10):
        rl = {s for s, ps in sender_pools.items() if len(ps) >= th}
        agg = [s for s in sw if s["s"] in rl]
        first_agg = agg[0]["b"] if agg else None
        adds_after = sum(1 for m in mods if m["dl"] > 0 and first_agg and m["b"] > first_agg)
        pokes_after = sum(1 for m in mods if m["dl"] == 0 and first_agg and m["b"] > first_agg)
        if not sw:
            cls = "ZERO_SWAP"
        elif not agg:
            cls = "SWAPPED_NON_ROUTER"
        elif pokes_after:
            cls = "FLOW_AND_HARVESTED"
        elif adds_after:
            cls = "FLOW_THEN_SCALED"
        elif len(agg) >= 2:
            cls = "ROUTER_LIKE_REPEAT"
        else:
            cls = "ROUTER_LIKE_ONE_OFF"
        e[f"outcome_t{th}"] = cls
        e[f"n_router_swaps_t{th}"] = len(agg)
    rows.append(e)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print(f"\nWrote {OUT}")

# ---- fixtures ------------------------------------------------------------
print("\n" + "=" * 72)
print("FIXTURES")
print("=" * 72)
lick = [r for r in rows if r["token"] == LICKLET]
lick_fees = sorted(int(r["fee"]) for r in lick)
exp = [770950 if False else f for f in (771000,)]
print(f"  LICKLET extreme pools: {len(lick)} (expect 9)")
print(f"    fee set: {[f/10000 for f in lick_fees]}")
print(f"    distinct PoolIds: {len({r['pool_id'] for r in lick})}")
fire = [r for r in rows if r["token"] == FIRE]
print(f"  FIRE extreme pools: {len(fire)}; contains 40% and 50%: "
      f"{ {400000,500000} <= {int(r['fee']) for r in fire} }")
seedfail = sum(1 for r in rows if r["seed_status"] == "UNSEEDED")
print(f"  UNSEEDED pools (legitimate, not retrieval failure): {seedfail}/{len(rows)} "
      f"({seedfail/len(rows)*100:.1f}%)")

# ---- rank matrix ---------------------------------------------------------
print("\n" + "=" * 72)
print("DEPLOYMENT RANK MATRIX (threshold 5)")
print("=" * 72)
print(f"  {'rank':6} {'pools':>6} {'any swap':>9} {'router-like':>12} {'repeat':>8} "
      f"{'scaled':>7} {'harvest':>8} {'% of router vol':>16}")
tot_rv = sum(int(r["n_router_swaps_t5"]) for r in rows) or 1
def bucket(k):
    k = int(k)
    return "1" if k == 1 else "2" if k == 2 else "3" if k == 3 else "4-5" if k <= 5 else "6-9" if k <= 9 else "10+"
grp = defaultdict(list)
for r in rows:
    grp[bucket(r["extreme_rank"])].append(r)
for b in ("1", "2", "3", "4-5", "6-9", "10+"):
    sub = grp.get(b)
    if not sub: continue
    anysw = sum(1 for r in sub if int(r["n_swaps"]) > 0)
    rl = sum(1 for r in sub if int(r["n_router_swaps_t5"]) > 0)
    rep = sum(1 for r in sub if r["outcome_t5"] in ("ROUTER_LIKE_REPEAT", "FLOW_THEN_SCALED", "FLOW_AND_HARVESTED"))
    sc = sum(1 for r in sub if r["outcome_t5"] == "FLOW_THEN_SCALED")
    hv = sum(1 for r in sub if r["outcome_t5"] == "FLOW_AND_HARVESTED")
    vol = sum(int(r["n_router_swaps_t5"]) for r in sub)
    print(f"  {b:6} {len(sub):>6} {anysw:>9} {rl:>12} {rep:>8} {sc:>7} {hv:>8} {vol/tot_rv*100:>15.1f}%")

print("\nRANK SENSITIVITY (share receiving router-like flow)")
print(f"  {'rank':6} {'t=3':>8} {'t=5':>8} {'t=10':>8}")
for b in ("1", "2", "3", "4-5", "6-9", "10+"):
    sub = grp.get(b)
    if not sub: continue
    cells = []
    for th in (3, 5, 10):
        w = sum(1 for r in sub if int(r[f"n_router_swaps_t{th}"]) > 0)
        cells.append(f"{w/len(sub)*100:.1f}%")
    print(f"  {b:6} {cells[0]:>8} {cells[1]:>8} {cells[2]:>8}")

# ---- winner fee relative to competitors ---------------------------------
print("\n" + "=" * 72)
print("WINNER FEE RELATIVE TO COMPETITORS (threshold 5)")
print("=" * 72)
import statistics
deltas, franks, tranks, n_multi = [], [], [], 0
for t, rs in by_tok.items():
    rs2 = [x for x in rows if x["token"] == t]
    if len(rs2) < 2: continue
    n_multi += 1
    win = max(rs2, key=lambda r: int(r["n_router_swaps_t5"]))
    if int(win["n_router_swaps_t5"]) == 0: continue
    fees = sorted(int(r["fee"]) for r in rs2)
    med = statistics.median(fees)
    deltas.append((int(win["fee"]) - med) / 10000)
    franks.append(sorted(fees).index(int(win["fee"])) + 1)
    tranks.append(int(win["extreme_rank"]))
print(f"  multi-pool opportunities: {n_multi}; with a router-like winner: {len(deltas)}")
if deltas:
    print(f"  winning fee - median competing fee: median {statistics.median(deltas):+.2f} pp, "
          f"range {min(deltas):+.2f} to {max(deltas):+.2f}")
    print(f"  winning fee rank within opportunity (1=lowest): {Counter(franks).most_common()}")
    print(f"  winning deployment rank: {Counter(tranks).most_common()}")
