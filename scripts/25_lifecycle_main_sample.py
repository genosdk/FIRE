"""Stage 2: ~700-token stratified lifecycle sample.

Competition stratification is deliberately preserved (100 / 250 / 175 / 175);
it is NOT rebalanced toward fee tiers, so the sample stays interpretable. If the
10-40% and 40-70% cells remain thin, a separate fee-balanced supplement should
be drawn rather than contaminating this one.

Two parallel success definitions are carried throughout:
  KNOWN_INFRA_FLOW  -- sender is identified router/settler infrastructure
  ROUTER_LIKE_FLOW  -- sender prevalence >= threshold (may include MM/arb bots)

Harvest attribution is operator-specific via the position salt: v4 position NFTs
in this ecosystem never transfer, so a zero-delta poke carrying the seed
position's tokenId is provably the same operator. A poke on a different salt is
another LP and is recorded as POOL_POKED_AFTER_FLOW.

Exact same-block ordering is deliberately deferred -- the validation rejected
latency as the dominant effect, so it is not worth a retrieval pass.
"""
import csv, hashlib, json, os, subprocess, sys, time
from collections import Counter, defaultdict

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
ML = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
OPPS = "data/analysis/CHAINWIDE_TOKEN_OPPORTUNITIES.csv"
CACHE = "data/analysis/_lifecycle_val_cache.json"        # shared with the validation run
OUT_POOL = "data/analysis/LIFECYCLE_MAIN_POOLS.csv"
OUT_TOK = "data/analysis/LIFECYCLE_MAIN_TOKENS.csv"

LICKLET = "0x23192efc86d38f8b9de39af603b73521ec346bcc"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
QUOTA = [(1, 1, 100), (2, 4, 250), (5, 9, 175), (10, 10 ** 9, 175)]
THRESHOLDS = (3, 5, 10)
DYN = 8388608

KNOWN_INFRA = {
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap UniversalRouter",
    "0x1d4b86491ec211257cbedd77a4380a7494624eff": "RobinHoodSettler",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7": "RobinHoodSettler",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f": "RobinHoodSettler",
    "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef": "Relay solver",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be": "RelayApprovalProxyV3",
    "0xa633c73658857d5241fe21d43f854a82e2d0592c": "router (unlabelled, high prevalence)",
    "0x8f10b468b06c6fd214b65f87778827f7d113f996": "router (unlabelled, high prevalence)",
}


def rpc(m, p, tries=8):
    """Retries with a much longer backoff on 429 -- sustained retrieval over
    thousands of pools trips the node's rate limiter, which is transient."""
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "60", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"])
                time.sleep(min(30, 4.0 * (a + 1)) if r["error"].get("code") == 429
                           else 0.4 * (a + 1))
                continue
            return r["result"]
        except Exception as e:
            last = e
            time.sleep(min(30, 4.0 * (a + 1)) if "429" in str(e) else 0.4 * (a + 1))
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


opps = list(csv.DictReader(open(OPPS)))
buckets = defaultdict(list)
for o in opps:
    n = int(o["n_extreme_pools"])
    for lo, hi, _ in QUOTA:
        if lo <= n <= hi:
            buckets[(lo, hi)].append(o); break
selected = {}
for lo, hi, k in QUOTA:
    ranked = sorted(buckets[(lo, hi)], key=lambda o: hashlib.sha256(o["token"].encode()).hexdigest())
    for o in ranked[:k]:
        selected[o["token"]] = o
for fx in (LICKLET, FIRE):
    hit = next((o for o in opps if o["token"] == fx), None)
    if hit: selected[fx] = hit
print(f"selected token opportunities: {len(selected)}")

want = set(selected)
pools = defaultdict(list)
for r in csv.DictReader(open(CENSUS)):
    if r["non_base_token"] in want and r["fee"] != str(DYN) and int(r["fee"]) >= 100000:
        pools[r["non_base_token"]].append(r)
n_pools = sum(len(v) for v in pools.values())
print(f"extreme pools to reconstruct: {n_pools:,}")

cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
print(f"already cached: {sum(1 for t in pools for p in pools[t] if p['pool_id'] in cache):,}")
latest = int(rpc("eth_blockNumber", []), 16)

flat = []
for tok, ps in pools.items():
    ps.sort(key=lambda r: int(r["init_block"]))
    for rank, p in enumerate(ps, 1):
        flat.append((tok, rank, len(ps), p))

done = 0
for tok, rank, ncomp, p in flat:
    pid = p["pool_id"]
    done += 1
    if pid in cache:
        continue
    init = int(p["init_block"])
    sw = logs_adaptive([SWAP, pid], init, latest)
    ml = logs_adaptive([ML, pid], init, latest)
    swaps, mods = [], []
    for L in sw:
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        swaps.append({"b": int(L["blockNumber"], 16), "s": ("0x" + L["topics"][2][26:]).lower(),
                      "a0": s128(w[0]), "a1": s128(w[1])})
    for L in ml:
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        mods.append({"b": int(L["blockNumber"], 16), "dl": s256(w[2]), "salt": int(w[3], 16)})
    cache[pid] = {"swaps": swaps, "mods": mods}
    time.sleep(0.15)
    if len(cache) % 50 == 0:
        json.dump(cache, open(CACHE, "w"))
        print(f"  {done:,}/{n_pools:,} (cache {len(cache):,})")
json.dump(cache, open(CACHE, "w"))
print(f"retrieval complete: {n_pools:,} pools")

sender_pools = defaultdict(set)
for tok, rank, ncomp, p in flat:
    for s in cache[p["pool_id"]]["swaps"]:
        sender_pools[s["s"]].add(p["pool_id"])
RL = {th: {s for s, ps in sender_pools.items() if len(ps) >= th} for th in THRESHOLDS}
print(f"\ndistinct senders {len(sender_pools):,}; router-like "
      + ", ".join(f"t{th}={len(RL[th]):,}" for th in THRESHOLDS))

rows = []
for tok, rank, ncomp, p in flat:
    rec = cache[p["pool_id"]]
    sw = sorted(rec["swaps"], key=lambda x: x["b"])
    mods = sorted(rec["mods"], key=lambda x: x["b"])
    seed = next((m for m in mods if m["dl"] > 0), None)
    seed_salt = seed["salt"] if seed else None
    known = [s for s in sw if s["s"] in KNOWN_INFRA]
    e = {"token": tok, "pool_id": p["pool_id"], "fee": int(p["fee"]),
         "tick_spacing": int(p["tick_spacing"]), "hooked": p["hooked"],
         "ts_sig": p["ts_equals_fee_over_100"], "fire_cohort": p["fire_cohort"],
         "init_block": int(p["init_block"]), "deployment_rank": rank, "n_competing": ncomp,
         "seed_status": "SEEDED" if seed else "UNSEEDED",
         "seed_salt": seed_salt or "", "n_swaps": len(sw), "n_mods": len(mods),
         "n_known_infra_swaps": len(known),
         "known_infra_flow": len(known) > 0}
    for th in THRESHOLDS:
        agg = [s for s in sw if s["s"] in RL[th]]
        fa = agg[0]["b"] if agg else None
        # operator-specific: only pokes on the SEED position count as harvesting
        own_pokes = sum(1 for m in mods if m["dl"] == 0 and fa and m["b"] > fa
                        and seed_salt is not None and m["salt"] == seed_salt)
        other_pokes = sum(1 for m in mods if m["dl"] == 0 and fa and m["b"] > fa
                          and (seed_salt is None or m["salt"] != seed_salt))
        own_adds = sum(1 for m in mods if m["dl"] > 0 and fa and m["b"] > fa
                       and seed_salt is not None and m["salt"] == seed_salt)
        if not sw: cls = "ZERO_SWAP"
        elif not agg: cls = "SWAPPED_NON_ROUTER"
        elif own_pokes: cls = "FLOW_AND_HARVESTED"
        elif own_adds: cls = "FLOW_THEN_SCALED"
        elif other_pokes: cls = "POOL_POKED_AFTER_FLOW"
        elif len(agg) >= 2: cls = "ROUTER_LIKE_REPEAT"
        else: cls = "ROUTER_LIKE_ONE_OFF"
        e[f"outcome_t{th}"] = cls
        e[f"n_router_swaps_t{th}"] = len(agg)
        e[f"router_like_flow_t{th}"] = len(agg) > 0
    rows.append(e)

with open(OUT_POOL, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

by_tok = defaultdict(list)
for r in rows: by_tok[r["token"]].append(r)
toks = []
for t, rs in by_tok.items():
    fees = sorted(r["fee"] for r in rs)
    lo_fee = fees[0]
    lows = [r for r in rs if r["fee"] == lo_fee]
    tot_rv = sum(r["n_router_swaps_t5"] for r in rs)
    win = max(rs, key=lambda r: r["n_router_swaps_t5"])
    has_win = win["n_router_swaps_t5"] > 0
    senders = set()
    for r in rs:
        for s in cache[r["pool_id"]]["swaps"]: senders.add(s["s"])
    toks.append({
        "token": t, "n_competing_pools": len(rs),
        "n_with_any_swap": sum(1 for r in rs if r["n_swaps"] > 0),
        "n_with_known_infra_flow": sum(1 for r in rs if r["known_infra_flow"]),
        "n_with_router_like_flow_t5": sum(1 for r in rs if r["router_like_flow_t5"]),
        "min_fee": lo_fee, "max_fee": fees[-1], "fee_spread": fees[-1] - lo_fee,
        "lowest_fee_pool_success": any(r["router_like_flow_t5"] for r in lows),
        "lowest_fee_pool_volume_share": (sum(r["n_router_swaps_t5"] for r in lows) / tot_rv
                                         if tot_rv else ""),
        "winner_fee": win["fee"] if has_win else "",
        "winner_fee_rank": (sorted(set(fees)).index(win["fee"]) + 1) if has_win else "",
        "winner_deployment_rank": win["deployment_rank"] if has_win else "",
        "winner_fee_minus_min_fee": (win["fee"] - lo_fee) if has_win else "",
        "n_distinct_senders": len(senders),
        "n_known_infra_senders": len(senders & set(KNOWN_INFRA)),
        "n_router_like_senders_t5": len(senders & RL[5]),
        "fire_cohort_pools": sum(1 for r in rs if r["fire_cohort"] == "True")})
with open(OUT_TOK, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(toks[0].keys())); w.writeheader(); w.writerows(toks)

print("\n" + "=" * 76)
print("SAFEGUARDS")
print("=" * 76)
fails = []
if len(rows) != n_pools: fails.append("completeness")
lick = [r for r in rows if r["token"] == LICKLET]
if lick and len(lick) != 9: fails.append(f"LICKLET fixture {len(lick)}")
fire = [r for r in rows if r["token"] == FIRE]
if fire and not {400000, 500000} <= {r["fee"] for r in fire}: fails.append("FIRE fixture")
uns = sum(1 for r in rows if r["seed_status"] == "UNSEEDED") / len(rows)
print(f"  pools {len(rows):,}/{n_pools:,}   LICKLET {len(lick)}   FIRE {len(fire)}   "
      f"UNSEEDED {uns:.1%}")
print(f"  RESULT: {'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
if fails: sys.exit(1)

print("\n" + "=" * 76)
print("OUTCOMES (threshold 5, operator-specific harvesting)")
print("=" * 76)
c = Counter(r["outcome_t5"] for r in rows)
for k in ("ZERO_SWAP", "SWAPPED_NON_ROUTER", "ROUTER_LIKE_ONE_OFF", "ROUTER_LIKE_REPEAT",
          "POOL_POKED_AFTER_FLOW", "FLOW_THEN_SCALED", "FLOW_AND_HARVESTED"):
    if c.get(k): print(f"  {k:24} {c[k]:>6,} ({c[k]/len(rows)*100:>5.1f}%)")
print(f"\n  ever swapped:            {sum(1 for r in rows if r['n_swaps']>0):,}/{len(rows):,} "
      f"({sum(1 for r in rows if r['n_swaps']>0)/len(rows)*100:.1f}%)")
print(f"  KNOWN_INFRA_FLOW:        {sum(1 for r in rows if r['known_infra_flow']):,} "
      f"({sum(1 for r in rows if r['known_infra_flow'])/len(rows)*100:.1f}%)")
for th in THRESHOLDS:
    n = sum(1 for r in rows if r[f"router_like_flow_t{th}"])
    print(f"  ROUTER_LIKE_FLOW t={th:<2}:     {n:,} ({n/len(rows)*100:.1f}%)")

print("\nSUCCESS BY FEE BAND")
print(f"  {'band':10} {'pools':>7} {'known infra':>13} {'router-like t5':>16}")
for lo, hi, lab in ((100000,400000,"10-40%"),(400000,700000,"40-70%"),
                    (700000,900000,"70-90%"),(900000,10**9,"90%+")):
    sub=[r for r in rows if lo<=r["fee"]<hi]
    if not sub: continue
    k=sum(1 for r in sub if r["known_infra_flow"]); q=sum(1 for r in sub if r["router_like_flow_t5"])
    print(f"  {lab:10} {len(sub):>7,} {k/len(sub)*100:>12.1f}% {q/len(sub)*100:>15.1f}%")

print("\nSUCCESS BY DEPLOYMENT RANK")
def bk(k): return "1" if k==1 else "2" if k==2 else "3" if k==3 else "4-5" if k<=5 else "6-9" if k<=9 else "10+"
g=defaultdict(list)
for r in rows: g[bk(r["deployment_rank"])].append(r)
tot_rv=sum(r["n_router_swaps_t5"] for r in rows) or 1
print(f"  {'rank':6} {'pools':>7} {'known infra':>13} {'router-like':>13} {'% router vol':>14}")
for b in ("1","2","3","4-5","6-9","10+"):
    sub=g.get(b)
    if not sub: continue
    k=sum(1 for r in sub if r["known_infra_flow"]); q=sum(1 for r in sub if r["router_like_flow_t5"])
    v=sum(r["n_router_swaps_t5"] for r in sub)
    print(f"  {b:6} {len(sub):>7,} {k/len(sub)*100:>12.1f}% {q/len(sub)*100:>12.1f}% {v/tot_rv*100:>13.1f}%")

print("\nLOWEST-FEE POOL vs COMPETITORS")
multi=[t for t in toks if t["n_competing_pools"]>1]
withwin=[t for t in multi if t["winner_fee"]!=""]
print(f"  multi-pool opportunities: {len(multi):,}; with a router-like winner: {len(withwin):,}")
if withwin:
    lw=sum(1 for t in withwin if t["lowest_fee_pool_success"])
    print(f"  lowest-fee pool received flow: {lw:,}/{len(withwin):,} ({lw/len(withwin)*100:.1f}%)")
    shares=[float(t["lowest_fee_pool_volume_share"]) for t in withwin if t["lowest_fee_pool_volume_share"]!=""]
    if shares:
        shares.sort()
        print(f"  lowest-fee pool volume share: median {shares[len(shares)//2]*100:.1f}%, "
              f"mean {sum(shares)/len(shares)*100:.1f}%")
    import statistics
    d=[(t["winner_fee_minus_min_fee"])/10000 for t in withwin]
    print(f"  winner fee - min fee: median {statistics.median(d):+.2f} pp")
    print(f"  winner fee rank (1=cheapest): {Counter(t['winner_fee_rank'] for t in withwin).most_common(6)}")
    print(f"  winner deployment rank: {Counter(t['winner_deployment_rank'] for t in withwin).most_common(6)}")
