"""Historical quote engine: what else could each routed order have executed against?

For every known-infrastructure swap, all pools for that token are replayed in
lockstep to the exact (block, txIndex, logIndex) immediately before the order,
then the OBSERVED input amount is simulated through each. That yields the
selected route's output alongside every alternative available at that instant.

PER-ROW INVARIANT: the simulator is first run against the SELECTED pool and must
reproduce the historical Swap event's output within 10 bps, otherwise the row is
discarded. A quotation bug would otherwise masquerade as router regret.

SCOPE NOTE (v1): comparisons are same-pair direct routes -- for a token/USDG
order, every other token/USDG pool including low-fee reference pools. 87% of
extreme pools are token/stable, so this covers most cases directly. Multi-leg
canonical routes (TOKEN -> ETH -> USDG) are not yet modelled; rows where the
token has no same-pair reference pool are flagged rather than guessed.
"""
import csv, hashlib, json, os, statistics, subprocess, sys, time
from collections import defaultdict
from decimal import Decimal, getcontext
sys.path.insert(0, "scripts")
from importlib import import_module
sr = import_module("27_state_replay")
getcontext().prec = 60

CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
MAIN = "data/analysis/LIFECYCLE_MAIN_POOLS.csv"
EVCACHE = "data/analysis/_quote_events_cache.json"
OUT = "data/analysis/HISTORICAL_QUOTE_QUALITY.csv"
N_TOKENS = 120
DYN = 8388608
LICKLET = "0x23192efc86d38f8b9de39af603b73521ec346bcc"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
KNOWN_INFRA = {
    "0x8876789976decbfcbbbe364623c63652db8c0904", "0x1d4b86491ec211257cbedd77a4380a7494624eff",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7", "0x39b38686a19836ac10162c490e4558e120cbbe5f",
    "0x0000000000001ff3684f28c67538d4d072c22734", "0xf70da97812cb96acdf810712aa562db8dfa3dbef",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be", "0xa633c73658857d5241fe21d43f854a82e2d0592c",
    "0x8f10b468b06c6fd214b65f87778827f7d113f996",
}

main = list(csv.DictReader(open(MAIN)))
by_tok = defaultdict(list)
for r in main:
    by_tok[r["token"]].append(r)
# stratify: fee band of the flow-receiving pool x competition x cheapest-won
cand = []
for t, rs in by_tok.items():
    if not any(r["known_infra_flow"] == "True" for r in rs):
        continue
    fees = sorted(int(r["fee"]) for r in rs)
    win = max(rs, key=lambda r: int(r["n_known_infra_swaps"]))
    band = ("10-40%" if int(win["fee"]) < 400000 else "40-70%" if int(win["fee"]) < 700000
            else "70-90%" if int(win["fee"]) < 900000 else "90%+")
    comp = "2-4" if len(rs) <= 4 else "5-9" if len(rs) <= 9 else "10+"
    cheapest_won = int(win["fee"]) == fees[0]
    cand.append((t, band, comp, cheapest_won))
strata = defaultdict(list)
for t, b, c, cw in cand:
    strata[(b, c, cw)].append(t)
sel = []
per = max(1, N_TOKENS // max(1, len(strata)))
for k in sorted(strata):
    ts = sorted(strata[k], key=lambda x: hashlib.sha256(x.encode()).hexdigest())
    sel += ts[:per]
extra = [t for t, *_ in cand if t not in sel]
extra.sort(key=lambda x: hashlib.sha256(x.encode()).hexdigest())
sel += extra[:max(0, N_TOKENS - len(sel))]
for fx in (FIRE, LICKLET):
    if fx in by_tok and fx not in sel:
        sel.append(fx)
sel = list(dict.fromkeys(sel))
print(f"strata: {len(strata)}  selected token opportunities: {len(sel)}")

# all pools for those tokens (every fee, so low-fee reference pools are included)
pools_for = defaultdict(list)
for r in csv.DictReader(open(CENSUS)):
    if r["non_base_token"] in set(sel) and r["fee"] != str(DYN):
        pools_for[r["non_base_token"]].append(r)
tot_pools = sum(len(v) for v in pools_for.values())
print(f"pools to replay: {tot_pools}")

cache = json.load(open(EVCACHE)) if os.path.exists(EVCACHE) else {}
latest = int(sr.rpc("eth_blockNumber", []), 16)
fetched = 0
for t, ps in pools_for.items():
    for p in ps:
        if p["pool_id"] in cache:
            continue
        ev = sr.fetch_pool_events(p["pool_id"], int(p["init_block"]), latest)
        cache[p["pool_id"]] = ev
        fetched += 1
        time.sleep(0.45)
        if fetched % 50 == 0:
            json.dump(cache, open(EVCACHE, "w"))
            print(f"  fetched {fetched} (cache {len(cache)})")
json.dump(cache, open(EVCACHE, "w"))
print(f"event retrieval complete ({len(cache)} pools cached)")

rows, inv_fail, inv_ok = [], 0, 0
for t in sel:
    ps = pools_for.get(t, [])
    if len(ps) < 2:
        continue
    meta = {p["pool_id"]: p for p in ps}
    states, init_ok = {}, True
    for p in ps:
        lg = None
        ev = cache.get(p["pool_id"], [])
        st = sr.PoolState(1, 0)
        states[p["pool_id"]] = {"st": None, "ev": ev, "i": 0, "p": p}
    # initialise each pool from its Initialize log data already in the census
    for p in ps:
        states[p["pool_id"]]["st"] = None
    # merged timeline
    timeline = []
    for p in ps:
        for e in cache.get(p["pool_id"], []):
            timeline.append((e["k"], p["pool_id"], e))
    timeline.sort(key=lambda x: x[0])
    live = {}
    for k, pid, e in timeline:
        p = meta[pid]
        if pid not in live:
            live[pid] = sr.PoolState(int(p["init_tick"]) * 0 + 1, int(p["init_tick"]))
            live[pid].sqrtP = sr.sqrt_at(int(p["init_tick"]))
        st = live[pid]
        if e["t"] == "ML":
            st.apply_ml(e["lo"], e["hi"], e["dl"])
            continue
        if e["sender"] in KNOWN_INFRA:
            zfo = e["a0"] < 0
            amt_in = abs(e["a0"]) if zfo else abs(e["a1"])
            actual_out = abs(e["a1"]) if zfo else abs(e["a0"])
            # invariant: reproduce the selected pool's own execution
            snap = sr.PoolState(1, st.tick); snap.sqrtP = st.sqrtP
            snap.net = dict(st.net); snap.recompute_active()
            sim_sel, _ = snap.swap_exact_in(zfo, amt_in, e["fee"])
            if actual_out > 0 and abs(sim_sel - Decimal(actual_out)) / Decimal(actual_out) > Decimal("0.001"):
                inv_fail += 1
            else:
                inv_ok += 1
                alts = []
                for qid, qst in live.items():
                    if qid == pid:
                        continue
                    q = meta[qid]
                    if (q["token0"], q["token1"]) != (p["token0"], p["token1"]):
                        continue
                    s2 = sr.PoolState(1, qst.tick); s2.sqrtP = qst.sqrtP
                    s2.net = dict(qst.net); s2.recompute_active()
                    if s2.L <= 0:
                        continue
                    o, _ = s2.swap_exact_in(zfo, amt_in, int(q["fee"]))
                    alts.append((o, qid, int(q["fee"])))
                best = max(alts, default=None)
                ref = max([a for a in alts if a[2] < 10000], default=None)
                sel_out = Decimal(actual_out)
                rows.append({
                    "token": t, "tx": e["tx"], "block": e["k"][0],
                    "selected_pool": pid, "selected_fee": e["fee"],
                    "input_amount": str(amt_in), "zero_for_one": zfo,
                    "selected_output": str(sel_out),
                    "sim_selected_output": str(sim_sel),
                    "n_alternatives": len(alts),
                    "best_competing_pool": best[1] if best else "",
                    "best_competing_fee": best[2] if best else "",
                    "best_competing_output": str(best[0]) if best else "",
                    "reference_pool_output": str(ref[0]) if ref else "",
                    "reference_pool_fee": ref[2] if ref else "",
                    "selection_regret_bps": (str((sel_out / best[0] - 1) * 10000)
                                             if best and best[0] > 0 else ""),
                    "vs_reference_bps": (str((sel_out / ref[0] - 1) * 10000)
                                         if ref and ref[0] > 0 else ""),
                })
        # advance selected pool to its authoritative post-state
        st.sqrtP = Decimal(e["sp"]) / sr.Q96
        st.tick = e["tick"]

if not rows:
    sys.exit("no rows produced")
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

print("\n" + "=" * 72)
print("PER-ROW INVARIANT (simulated selected output vs actual)")
print("=" * 72)
tot = inv_ok + inv_fail
print(f"  passed within 10 bps: {inv_ok}/{tot} ({inv_ok/tot*100:.1f}%)")
print(f"  discarded:            {inv_fail}")
if tot and inv_fail / tot > 0.05:
    sys.exit(f"ABORT: {inv_fail/tot:.1%} of rows failed the quotation invariant")

reg = sorted(Decimal(r["selection_regret_bps"]) for r in rows if r["selection_regret_bps"])
print("\n" + "=" * 72)
print("KNOWN-INFRA HISTORICAL SELECTION QUALITY")
print("=" * 72)
print(f"  routed orders analysed: {len(rows)}  with >=1 alternative: {len(reg)}")
if reg:
    def q(p): return reg[min(len(reg) - 1, int(len(reg) * p))]
    print(f"\n  selected vs best available (bps; 0 = optimal, negative = worse)")
    print(f"    median {q(0.5):+.1f}   p25 {q(0.25):+.1f}   p10 {q(0.10):+.1f}   worst {reg[0]:+.1f}")
    for th in (10, 25, 50, 100, 500):
        n = sum(1 for x in reg if x >= -th)
        print(f"    within {th:>4} bps of best: {n}/{len(reg)} ({n/len(reg)*100:.1f}%)")
    n = sum(1 for x in reg if x < -500)
    print(f"    worse than 500 bps:      {n}/{len(reg)} ({n/len(reg)*100:.1f}%)")
    print("\n  BY FEE BAND OF SELECTED POOL")
    print(f"    {'band':10} {'orders':>7} {'median bps':>12} {'% within 25':>13} {'% within 100':>14}")
    for lo, hi, lab in ((100000,400000,"10-40%"),(400000,700000,"40-70%"),
                        (700000,900000,"70-90%"),(900000,10**9,"90%+")):
        sub = sorted(Decimal(r["selection_regret_bps"]) for r in rows
                     if r["selection_regret_bps"] and lo <= int(r["selected_fee"]) < hi)
        if not sub: continue
        w25 = sum(1 for x in sub if x >= -25); w100 = sum(1 for x in sub if x >= -100)
        print(f"    {lab:10} {len(sub):>7} {sub[len(sub)//2]:>+11.1f} "
              f"{w25/len(sub)*100:>12.1f}% {w100/len(sub)*100:>13.1f}%")
vref = sorted(Decimal(r["vs_reference_bps"]) for r in rows if r["vs_reference_bps"])
if vref:
    print(f"\n  selected vs low-fee reference pool (bps): median {vref[len(vref)//2]:+.1f}, "
          f"n={len(vref)}")
    n = sum(1 for x in vref if x > 0)
    print(f"    selected BEAT the reference pool: {n}/{len(vref)} ({n/len(vref)*100:.1f}%)")
