"""Stage 1: 50-token lifecycle validation run.

Reconstructs the full lifecycle of every extreme-fee pool belonging to a small
stratified set of token opportunities, before committing query budget to the
~700-token main sample. Carries the four safeguards adopted after the batch
failure: retrieval completeness, known-answer fixtures (LICKLET and FIRE),
null-rate ceiling on critical fields, and nonzero output.

Aggregator classification is self-calibrating: a swap_sender seen across many
distinct pools is infrastructure, whichever protocol it belongs to. That avoids
hard-coding a router list that would silently miss families we haven't met.
"""
import csv, hashlib, json, os, subprocess, sys, time
from collections import Counter, defaultdict

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
ML = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
OPPS = "data/analysis/CHAINWIDE_TOKEN_OPPORTUNITIES.csv"
OUT_POOL = "data/analysis/LIFECYCLE_VALIDATION_POOLS.csv"
OUT_TOK = "data/analysis/LIFECYCLE_VALIDATION_TOKENS.csv"
CACHE = "data/analysis/_lifecycle_val_cache.json"

LICKLET = "0x23192efc86d38f8b9de39af603b73521ec346bcc"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
QUOTA = [(1, 1, 10), (2, 4, 15), (5, 9, 10), (10, 10**9, 15)]
ROUTER_MIN_POOLS = 5     # a sender seen in >=5 distinct pools is treated as infrastructure


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


def logs_adaptive(topics, lo, hi, depth=0):
    try:
        return rpc("eth_getLogs", [{"address": PM, "fromBlock": hex(lo), "toBlock": hex(hi),
                                    "topics": topics}], tries=2)
    except Exception as e:
        if "exceeds limit" not in str(e) or lo >= hi or depth > 12:
            raise
        mid = (lo + hi) // 2
        return logs_adaptive(topics, lo, mid, depth + 1) + logs_adaptive(topics, mid + 1, hi, depth + 1)


def s128(w):
    v = int(w, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >= 1 << 127 else v


def s256(w):
    v = int(w, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


# ---- stratified, deterministic selection --------------------------------
opps = list(csv.DictReader(open(OPPS)))
print(f"token opportunities available: {len(opps):,}")
buckets = defaultdict(list)
for o in opps:
    n = int(o["n_extreme_pools"])
    for lo, hi, _ in QUOTA:
        if lo <= n <= hi:
            buckets[(lo, hi)].append(o)
            break
selected = {}
for lo, hi, k in QUOTA:
    pool = sorted(buckets[(lo, hi)],
                  key=lambda o: hashlib.sha256(o["token"].encode()).hexdigest())
    for o in pool[:k]:
        selected[o["token"]] = o
for fixture in (LICKLET, FIRE):
    hit = next((o for o in opps if o["token"] == fixture), None)
    if hit:
        selected[fixture] = hit
print(f"selected token opportunities: {len(selected)} (incl. fixtures)")

# ---- their extreme pools ------------------------------------------------
DYN = 8388608
want = set(selected)
pools = defaultdict(list)
for r in csv.DictReader(open(CENSUS)):
    if r["non_base_token"] in want and r["fee"] != str(DYN) and int(r["fee"]) >= 100000:
        pools[r["non_base_token"]].append(r)
n_pools = sum(len(v) for v in pools.values())
print(f"extreme pools to reconstruct: {n_pools}")

cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
latest = int(rpc("eth_blockNumber", []), 16)

rows = []
done = 0
for tok, ps in pools.items():
    ps.sort(key=lambda r: int(r["init_block"]))
    for rank, p in enumerate(ps, 1):
        pid = p["pool_id"]
        if pid in cache:
            rec = cache[pid]
        else:
            init = int(p["init_block"])
            sw = logs_adaptive([SWAP, pid], init, latest)
            ml = logs_adaptive([ML, pid], init, latest)
            swaps = []
            for L in sw:
                d = L["data"][2:]
                w = [d[i:i + 64] for i in range(0, len(d), 64)]
                swaps.append({"b": int(L["blockNumber"], 16),
                              "s": ("0x" + L["topics"][2][26:]).lower(),
                              "a0": s128(w[0]), "a1": s128(w[1])})
            mods = []
            for L in ml:
                d = L["data"][2:]
                w = [d[i:i + 64] for i in range(0, len(d), 64)]
                mods.append({"b": int(L["blockNumber"], 16), "dl": s256(w[2]),
                             "salt": int(w[3], 16)})
            rec = {"swaps": swaps, "mods": mods}
            cache[pid] = rec
            if len(cache) % 25 == 0:
                json.dump(cache, open(CACHE, "w"))
        rows.append({"token": tok, "pool_id": pid, "fee": int(p["fee"]),
                     "tick_spacing": int(p["tick_spacing"]), "hooked": p["hooked"],
                     "ts_sig": p["ts_equals_fee_over_100"],
                     "fire_cohort": p["fire_cohort"], "init_block": int(p["init_block"]),
                     "deployment_rank": rank, "n_competing": len(ps),
                     "_rec": rec})
        done += 1
        if done % 25 == 0:
            print(f"  {done}/{n_pools}")
json.dump(cache, open(CACHE, "w"))

# ---- self-calibrating router detection ----------------------------------
sender_pools = defaultdict(set)
for r in rows:
    for s in r["_rec"]["swaps"]:
        sender_pools[s["s"]].add(r["pool_id"])
ROUTERS = {s for s, ps in sender_pools.items() if len(ps) >= ROUTER_MIN_POOLS}
print(f"\nswap senders seen: {len(sender_pools)}; classified as infrastructure "
      f"(>= {ROUTER_MIN_POOLS} distinct pools): {len(ROUTERS)}")

out = []
for r in rows:
    rec = r.pop("_rec")
    sw = sorted(rec["swaps"], key=lambda x: x["b"])
    mods = sorted(rec["mods"], key=lambda x: x["b"])
    agg = [s for s in sw if s["s"] in ROUTERS]
    seed_b = mods[0]["b"] if mods else None
    first_swap = sw[0]["b"] if sw else None
    first_agg = agg[0]["b"] if agg else None
    adds_after = sum(1 for m in mods if m["dl"] > 0 and first_agg and m["b"] > first_agg)
    pokes_after = sum(1 for m in mods if m["dl"] == 0 and first_agg and m["b"] > first_agg)
    if not sw:
        cls = "ZERO_SWAP"
    elif not agg:
        cls = "SWAPPED_NON_AGGREGATOR"
    elif pokes_after:
        cls = "FLOW_AND_HARVESTED"
    elif adds_after:
        cls = "FLOW_THEN_SCALED"
    elif len(agg) >= 2:
        cls = "AGGREGATOR_REPEAT"
    else:
        cls = "AGGREGATOR_ONE_OFF"
    out.append({**r, "outcome": cls, "n_swaps": len(sw), "n_agg_swaps": len(agg),
                "n_mods": len(mods), "n_pokes": sum(1 for m in mods if m["dl"] == 0),
                "seed_block": seed_b,
                "blocks_init_to_seed": (seed_b - r["init_block"]) if seed_b else "",
                "blocks_init_to_first_swap": (first_swap - r["init_block"]) if first_swap else "",
                "blocks_init_to_first_agg": (first_agg - r["init_block"]) if first_agg else "",
                "adds_after_flow": adds_after, "pokes_after_flow": pokes_after})

with open(OUT_POOL, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)

toks = []
for tok, ps in pools.items():
    sub = [o for o in out if o["token"] == tok]
    winners = [o for o in sub if o["n_agg_swaps"] > 0]
    best = max(sub, key=lambda o: o["n_agg_swaps"]) if sub else None
    first_route = min(( o for o in sub if o["blocks_init_to_first_agg"] != ""),
                      key=lambda o: o["init_block"] + o["blocks_init_to_first_agg"], default=None)
    toks.append({"token": tok, "n_competing_pools": len(sub),
                 "n_pools_with_any_swap": sum(1 for o in sub if o["n_swaps"] > 0),
                 "n_pools_with_agg_flow": len(winners),
                 "first_to_market_pool": min(sub, key=lambda o: o["init_block"])["pool_id"],
                 "first_to_route_pool": first_route["pool_id"] if first_route else "",
                 "highest_agg_volume_pool": best["pool_id"] if best else "",
                 "winning_rank": best["deployment_rank"] if best and best["n_agg_swaps"] else "",
                 "winning_fee": best["fee"] if best and best["n_agg_swaps"] else "",
                 "fire_cohort_pools": sum(1 for o in sub if o["fire_cohort"] == "True")})
with open(OUT_TOK, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(toks[0].keys())); w.writeheader(); w.writerows(toks)

# ---- safeguards ----------------------------------------------------------
print("\n" + "=" * 70)
print("SAFEGUARDS")
print("=" * 70)
fails = []
if len(out) != n_pools: fails.append(f"completeness {len(out)}/{n_pools}")
lick = [o for o in out if o["token"] == LICKLET]
if lick:
    traded = sum(1 for o in lick if o["n_swaps"] > 0)
    print(f"  fixture LICKLET: {len(lick)} extreme pools (expect 9), {traded} traded (expect 2)")
    if len(lick) != 9 or traded != 2: fails.append("LICKLET fixture")
fire = [o for o in out if o["token"] == FIRE]
if fire:
    print(f"  fixture FIRE: {len(fire)} extreme pools; "
          f"{sum(1 for o in fire if o['fee'] in (400000,500000))} at 40%/50%")
    if not any(o["fee"] == 500000 for o in fire): fails.append("FIRE fixture")
nullrate = sum(1 for o in out if o["seed_block"] is None) / len(out)
print(f"  null seed_block rate: {nullrate:.1%}")
if nullrate > 0.15: fails.append(f"seed null-rate {nullrate:.1%}")
if not out: fails.append("empty output")
print(f"  RESULT: {'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
if fails: sys.exit(1)

print("\n" + "=" * 70)
print("LIFECYCLE OUTCOMES")
print("=" * 70)
c = Counter(o["outcome"] for o in out)
for k in ("ZERO_SWAP", "SWAPPED_NON_AGGREGATOR", "AGGREGATOR_ONE_OFF", "AGGREGATOR_REPEAT",
          "FLOW_THEN_SCALED", "FLOW_AND_HARVESTED"):
    if c.get(k): print(f"  {k:26} {c[k]:>5} ({c[k]/len(out)*100:>5.1f}%)")
print(f"\n  ever swapped:        {sum(1 for o in out if o['n_swaps']>0)}/{len(out)} "
      f"({sum(1 for o in out if o['n_swaps']>0)/len(out)*100:.1f}%)")
print(f"  ever aggregator flow:{sum(1 for o in out if o['n_agg_swaps']>0)}/{len(out)} "
      f"({sum(1 for o in out if o['n_agg_swaps']>0)/len(out)*100:.1f}%)")
print("\nSUCCESS BY DEPLOYMENT RANK")
for r in range(1, 7):
    sub = [o for o in out if o["deployment_rank"] == r]
    if not sub: continue
    w = sum(1 for o in sub if o["n_agg_swaps"] > 0)
    print(f"  rank {r}: {w}/{len(sub)} received aggregator flow ({w/len(sub)*100:.1f}%)")
late = [o for o in out if o["deployment_rank"] >= 7]
if late:
    w = sum(1 for o in late if o["n_agg_swaps"] > 0)
    print(f"  rank 7+: {w}/{len(late)} ({w/len(late)*100:.1f}%)")
print("\nSUCCESS BY FEE BAND")
for lo, hi, lab in ((100000,400000,"10-40%"),(400000,700000,"40-70%"),
                    (700000,900000,"70-90%"),(900000,10**9,"90%+")):
    sub=[o for o in out if lo<=o["fee"]<hi]
    if not sub: continue
    w=sum(1 for o in sub if o["n_agg_swaps"]>0)
    print(f"  {lab:8} {w}/{len(sub)} ({w/len(sub)*100:>5.1f}%)")
