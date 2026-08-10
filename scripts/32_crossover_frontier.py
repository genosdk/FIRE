"""Exact economic crossover: what state must an extreme-fee pool be in to win?

For every known-infrastructure order, every simultaneously live extreme-fee
(>=10%) same-pair pool is treated as a synthetic TARGET and its price is
displaced -- tick-liquidity map held fixed, so every trial state is one a real
trade could have produced -- until it produces exactly the same output as the
best KNOWN-ELIGIBLE competitor at that instant:

    F(P) = Y_target(x; P) - Y_best(x) = 0

Search runs in a signed favourability coordinate u (u = tick when the trader
sells token0, u = -tick when it sells token1), so "larger u" always means
"better for the trader" and the bracket is chosen by the SIGN of F at the
pool's actual state:

    F(actual) <  0   pool is uncompetitive -> crossover lies ABOVE  (raise u)
    F(actual) >= 0   pool is already routeable -> crossover lies BELOW (lower u)

Most routed winners are already routeable, so the second branch is the common
one and a one-directional search would silently return the wrong answer.

The fee-only heuristics 1/(1-f) and (1-f_comp)/(1-f_target) are computed too,
but only as decomposition terms. The crossover itself comes from the validated
v4 swap engine in 27_state_replay.py, tick crossings and all.

CONFIDENCE SCOPE: both the synthetic target and its competitors are restricted
to hookless, static-fee pools. Replaying a hooked pool establishes what its hook
did on the calls it actually saw; it does not establish what the hook would
return at a price the pool never traded at.
"""
import bisect, csv, json, math, os, statistics, sys
from collections import defaultdict
from decimal import Decimal, getcontext
sys.path.insert(0, "scripts")
from importlib import import_module
sr = import_module("27_state_replay")
getcontext().prec = 60

CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
QUOTES = "data/analysis/HISTORICAL_QUOTE_QUALITY.csv"
EVCACHE = "data/analysis/_quote_events_cache.json"
OUT = "data/analysis/CROSSOVER_FRONTIER.csv"
CANDOUT = "data/analysis/ORDER_CANDIDATE_SET.csv"
SUMMARY = "data/analysis/CROSSOVER_FRONTIER_SUMMARY.csv"

DYN = 8388608
EXTREME = 100000          # >=10% -- the population the frontier is about
LOWFEE = 10000            # <1% -- "reference displayed price"
MIN_TICK, MAX_TICK = -887272, 887272
SPAN = 1 << 21            # doubling schedule ceiling, wider than the tick range
MAX_STEPS = 4096          # tick-crossing cap for synthetic displacement

ONLY = os.environ.get("CF_ONLY_TOKENS", "").strip()
if ONLY:                              # single-token reruns (script 34 uses this)
    OUT = OUT.replace(".csv", "_SUBSET.csv")
    CANDOUT = CANDOUT.replace(".csv", "_SUBSET.csv")
    SUMMARY = SUMMARY.replace(".csv", "_SUBSET.csv")

FAMILY = {
    "0x1d4b86491ec211257cbedd77a4380a7494624eff": "0x Settler",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7": "0x Settler",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f": "0x Settler",
    "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap UniversalRouter",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef": "Relay solver",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be": "Relay proxy",
    "0xa633c73658857d5241fe21d43f854a82e2d0592c": "unlabelled router A",
    "0x8f10b468b06c6fd214b65f87778827f7d113f996": "unlabelled router B",
}
KNOWN_INFRA = set(FAMILY)
ONE = Decimal(1)
M = Decimal(1_000_000)
LN_BASE = sr.BASE.ln()


class FastState(sr.PoolState):
    """PoolState with the tick index precomputed once per (order, target).

    The base class re-sorts the tick map on every next_tick/recompute_active
    call, which is fine for a single replay but not for ~100 evaluations inside
    a binary search. Behaviour is identical -- same ticks, same active
    liquidity -- only the lookup is O(log n) instead of O(n log n).
    """

    def bind(self, net, ts, pref):
        self.net = net
        self._ts = ts
        self._pref = pref
        return self

    def next_tick(self, zero_for_one):
        i = bisect.bisect_right(self._ts, self.tick)
        if zero_for_one:
            return self._ts[i - 1] if i else None
        return self._ts[i] if i < len(self._ts) else None

    def recompute_active(self):
        self.L = self._pref[bisect.bisect_right(self._ts, self.tick)]
        return self.L

    def seat(self, tick, sqrtP):
        self.tick = tick
        self.sqrtP = sqrtP
        self.recompute_active()
        return self

    def seat_x(self, x):
        """Place the pool at real-valued log-price coordinate x (tick units)."""
        return self.seat(int(math.floor(x)), sr.BASE ** (Decimal(x) / 2))


def tick_index(net):
    ts = sorted(t for t in net if net[t] != 0)
    pref, acc = [0], 0
    for t in ts:
        acc += net[t]
        pref.append(acc)
    return ts, pref


def x_of(sqrtP):
    """Real-valued tick coordinate of a sqrt price."""
    return float(2 * sqrtP.ln() / LN_BASE)


def g(x, sig=12):
    """Compact fixed-significance output. The engine carries 60 digits so the
    root search converges cleanly; nothing downstream means more than ~12, and
    at 144k rows the difference is 250 MB versus 60 MB on disk."""
    return "" if x is None else f"{Decimal(x):.{sig}g}"


def spot_out_per_in(sqrtP, zfo):
    """Price in OUTPUT token per INPUT token, so larger is always better.

    Un-decimal-adjusted. Every multiple reported here is a ratio of two prices
    on the SAME token pair, so the decimal factor cancels exactly.
    """
    p = sqrtP * sqrtP
    return p if zfo else ONE / p


# ---------------------------------------------------------------- reporting
BANDS = [(EXTREME, 400000, "10-40%"), (400000, 700000, "40-70%"),
         (700000, 900000, "70-90%"), (900000, 10 ** 9, "90%+")]
SIZES = [(0, 5, "<5bps"), (5, 25, "5-25"), (25, 100, "25-100"), (100, 500, "100-500")]
CREDIBLE = 500.0     # benchmark price impact ceiling, in bps


def _q(xs, p):
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def _col(rs, k):
    return sorted(float(r[k]) for r in rs if r[k] != "")


def report(path=None):
    allr = list(csv.DictReader(open(path or OUT)))
    rs = [r for r in allr if r["status"] == "OK"]
    if not rs:
        sys.exit("no OK rows to report")
    nx = sum(1 for r in allr if r["status"] == "NO_CROSSOVER_BELOW_CEILING")
    nu = sum(1 for r in allr if r["status"] == "NO_UNIQUE_ROOT")
    imp = _col(rs, "benchmark_impact_bps")
    thin = sum(1 for v in imp if v >= CREDIBLE)
    print("\n" + "=" * 78)
    print("CROSSOVER FRONTIER")
    print("=" * 78)
    print(f"  rows {len(rs):,}   distinct orders {len({r['order_key'] for r in rs}):,}"
          f"   tokens {len({r['token'] for r in rs})}")
    print(f"  benchmark own price impact: median {_q(imp,0.5):.0f} bps   "
          f"p90 {_q(imp,0.9):.0f} bps")
    print(f"  benchmark thinner than {CREDIBLE:.0f} bps impact: {thin:,}/{len(rs):,} "
          f"({thin/len(rs)*100:.1f}%) -- EXCLUDED from the tables below, because a")
    print(f"    book that moves that far on one order is not a reference price.")
    print(f"  no crossover at any price (target book too small to ever match): {nx:,}")
    print(f"  no unique root (F flat at zero across a tick):                    {nu:,}")

    cred = [r for r in rs if float(r["benchmark_impact_bps"]) < CREDIBLE]

    def band_of(r):
        f = int(r["target_fee"])
        return next(lab for lo, hi, lab in BANDS if lo <= f < hi)

    print("\n  REQUIRED PRICE MULTIPLE, decomposed (medians; credible benchmarks only)")
    print("    naive -> +pairwise fee -> +competitor slippage -> EXACT. The last")
    print("    step is the target's OWN depth, shown separately as 'depth x'.")
    print(f"    {'band':8} {'n':>6} {'naive':>8} {'+pair fee':>10} {'+comp slip':>11} "
          f"{'depth x':>9} {'EXACT':>10} {'actual':>8} {'headroom bps':>13}")
    for lo, hi, lab in BANDS:
        g = [r for r in cred if lo <= int(r["target_fee"]) < hi]
        if not g:
            continue
        depth = sorted(float(r["required_multiple_vs_benchmark"]) /
                       float(r["no_target_slip_multiple"]) for r in g
                       if r["required_multiple_vs_benchmark"])
        print(f"    {lab:8} {len(g):>6} {_q(_col(g,'naive_multiple'),.5):>8.3f} "
              f"{_q(_col(g,'pairwise_fee_only_multiple'),.5):>10.3f} "
              f"{_q(_col(g,'no_target_slip_multiple'),.5):>11.3f} "
              f"{_q(depth,.5):>9.2f} "
              f"{_q(_col(g,'required_multiple_vs_benchmark'),.5):>10.3f} "
              f"{_q(_col(g,'actual_multiple_vs_benchmark'),.5):>8.3f} "
              f"{_q(_col(g,'headroom_price_bps'),.5):>13.0f}")

    print("\n  EXACT REQUIRED MULTIPLE -- distribution, not just the median")
    print(f"    {'band':8} {'n':>6} {'p10':>8} {'p25':>8} {'p50':>8} {'p75':>8} {'p90':>8}")
    for lo, hi, lab in BANDS:
        g = _col([r for r in cred if lo <= int(r["target_fee"]) < hi],
                 "required_multiple_vs_benchmark")
        if not g:
            continue
        print(f"    {lab:8} {len(g):>6} " + " ".join(f"{_q(g,p):>8.3f}"
              for p in (.10, .25, .50, .75, .90)))

    print("\n  THRESHOLD SURFACE -- median exact required multiple, fee x order size")
    print("    Order size is the price impact the order has on the benchmark book,")
    print(f"    which the {CREDIBLE:.0f} bps credibility filter also caps -- so this surface")
    print("    covers small-to-moderate orders only, not the large-order regime.")
    print(f"    {'band':8} " + " ".join(f"{lab:>12}" for _, _, lab in SIZES))
    for lo, hi, lab in BANDS:
        cells = []
        for slo, shi, _ in SIZES:
            g = _col([r for r in cred if lo <= int(r["target_fee"]) < hi
                      and slo <= float(r["benchmark_impact_bps"]) < shi],
                     "required_multiple_vs_benchmark")
            cells.append(f"{_q(g,.5):>7.2f}x/{len(g):<4}" if g else f"{'-':>12}")
        print(f"    {lab:8} " + " ".join(cells))

    print("\n  ROUTEABILITY MARGIN AT THE ACTUAL STATE (bps vs best eligible competitor)")
    for lab, g in (("target WAS selected", [r for r in cred if r["target_is_selected"] == "True"]),
                   ("target NOT selected", [r for r in cred if r["target_is_selected"] == "False"])):
        m = _col(g, "routeability_margin_bps")
        if not m:
            continue
        pos = sum(1 for v in m if v > 0)
        print(f"    {lab:22} n={len(m):>6}  median {_q(m,.5):>+10.1f}  "
              f"p10 {_q(m,.10):>+10.1f}  p90 {_q(m,.90):>+10.1f}  above crossover {pos/len(m)*100:5.1f}%")

    # compact committable digest -- the row-level CSV is too large for the repo
    srows = []
    for lo, hi, lab in BANDS:
        for slo, shi, slab in [(0, 10 ** 9, "all")] + SIZES:
            g2 = [r for r in cred if lo <= int(r["target_fee"]) < hi
                  and slo <= float(r["benchmark_impact_bps"]) < shi]
            if not g2:
                continue
            req = _col(g2, "required_multiple_vs_benchmark")
            srows.append({
                "fee_band": lab, "order_size_bucket_bps": slab, "n": len(g2),
                "median_naive_multiple": f"{_q(_col(g2,'naive_multiple'),.5):.4f}",
                "median_pairwise_multiple": f"{_q(_col(g2,'pairwise_fee_only_multiple'),.5):.4f}",
                "median_no_target_slip_multiple": f"{_q(_col(g2,'no_target_slip_multiple'),.5):.4f}",
                "p10_exact_required": f"{_q(req,.10):.4f}",
                "p25_exact_required": f"{_q(req,.25):.4f}",
                "median_exact_required": f"{_q(req,.5):.4f}",
                "p75_exact_required": f"{_q(req,.75):.4f}",
                "p90_exact_required": f"{_q(req,.90):.4f}",
                "median_actual_multiple": f"{_q(_col(g2,'actual_multiple_vs_benchmark'),.5):.4f}",
                "median_headroom_bps": f"{_q(_col(g2,'headroom_price_bps'),.5):.1f}",
                "median_margin_bps": f"{_q(_col(g2,'routeability_margin_bps'),.5):.1f}",
                "share_above_crossover": f"{sum(1 for v in _col(g2,'routeability_margin_bps') if v>0)/len(g2):.4f}",
            })
    with open(SUMMARY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(srows[0].keys())); w.writeheader(); w.writerows(srows)
    print(f"\n  digest written: {SUMMARY} ({len(srows)} cells)")

    print("\n  HOW WRONG IS THE price x (1-fee) HEURISTIC? (exact / naive, medians)")
    print(f"    {'band':8} {'n':>6} {'exact/naive':>12} {'exact/pairwise':>15}")
    for lo, hi, lab in BANDS:
        g = [r for r in cred if lo <= int(r["target_fee"]) < hi and r["required_multiple_vs_benchmark"]]
        if not g:
            continue
        a = sorted(float(r["required_multiple_vs_benchmark"]) / float(r["naive_multiple"]) for r in g)
        b = sorted(float(r["required_multiple_vs_benchmark"]) / float(r["pairwise_fee_only_multiple"])
                   for r in g)
        print(f"    {lab:8} {len(g):>6} {_q(a,.5):>12.3f} {_q(b,.5):>15.3f}")


if "--report-only" in sys.argv:
    report()
    sys.exit(0)

# ---------------------------------------------------------------- inputs
toks = {r["token"] for r in csv.DictReader(open(QUOTES))}
if ONLY:
    toks &= {a.strip().lower() for a in ONLY.split(",")}
    print(f"CF_ONLY_TOKENS set -> {len(toks)} token(s), writing {OUT}")
pools_for = defaultdict(list)
meta = {}
for r in csv.DictReader(open(CENSUS)):
    if r["non_base_token"] in toks and r["fee"] != str(DYN):
        pools_for[r["non_base_token"]].append(r)
        meta[r["pool_id"]] = r
print(f"tokens {len(toks)}  static-fee pools {len(meta)}")

cache = json.load(open(EVCACHE))
for ev in cache.values():
    for e in ev:
        if isinstance(e.get("k"), list):
            e["k"] = tuple(e["k"])

# GATE 1 -- retrieval completeness
need = [p["pool_id"] for ps in pools_for.values() for p in ps]
have = sum(1 for p in need if p in cache)
print(f"GATE 1 event retrieval: {have}/{len(need)} pools cached ({have/len(need)*100:.2f}%)")
if have / len(need) < 0.99:
    sys.exit("ABORT: event cache below the 99% retrieval threshold")

# prior-use index: pool -> family -> sorted event keys, for the eligibility test
fam_keys = defaultdict(lambda: defaultdict(list))
tx_pair_pools = defaultdict(set)
for pid, ev in cache.items():
    m = meta.get(pid)
    for e in ev:
        if e["t"] != "SWAP":
            continue
        fam_keys[pid][FAMILY.get(e["sender"], "other")].append(e["k"])
        if m:
            tx_pair_pools[(e["tx"], m["token0"], m["token1"])].add(pid)
for pid in fam_keys:
    for f in fam_keys[pid]:
        fam_keys[pid][f].sort()


def used_before(pid, fam, okey):
    ks = fam_keys.get(pid, {}).get(fam)
    if not ks:
        return False
    return bisect.bisect_left(ks, okey) > 0


# ---------------------------------------------------------------- crossover
def crossover(fs, zfo, amt_in, fee_pips, y_best, x_actual):
    """Bracket by the sign of F at the actual state, then bisect. Returns dict."""
    def F(u):
        x = u if zfo else -u
        fs.seat_x(x)
        y, _ = fs.swap_exact_in(zfo, amt_in, fee_pips, max_steps=MAX_STEPS)
        return y - y_best, fs.unfilled_frac

    u0 = x_actual if zfo else -x_actual
    f0, _ = F(u0)
    lo = hi = None
    if f0 >= 0:                       # already routeable: crossover lies BELOW
        hi = u0
        step = 1.0
        while step <= SPAN:
            c = max(float(MIN_TICK), u0 - step)
            fc, _ = F(c)
            if fc < 0:
                lo = c
                break
            hi = c
            if c <= MIN_TICK:
                break
            step *= 2
        if lo is None:
            return {"status": "NO_CROSSOVER_BELOW_FLOOR", "u": None}
    else:                             # uncompetitive: crossover lies ABOVE
        lo = u0
        step = 1.0
        while step <= SPAN:
            c = min(float(MAX_TICK), u0 + step)
            fc, unf = F(c)
            if fc >= 0:
                hi = c
                break
            lo = c
            if c >= MAX_TICK:
                break
            step *= 2
        if hi is None:
            return {"status": "NO_CROSSOVER_BELOW_CEILING", "u": None}

    # monotonicity of F in u over the bracket (plateaus allowed, reversals not)
    mono = True
    prev = None
    tol = -(abs(y_best) * Decimal("1e-12") + Decimal("1e-9"))
    for i in range(9):
        fu, _ = F(lo + (hi - lo) * i / 8.0)
        if prev is not None and fu - prev < tol:
            mono = False
            break
        prev = fu

    for _ in range(120):
        if hi - lo <= 1e-7:
            break
        mid = (lo + hi) / 2.0
        fm, _ = F(mid)
        if fm < 0:
            lo = mid
        else:
            hi = mid
    u = (lo + hi) / 2.0
    resid, unf = F(u)
    # a plateau at zero has no unique root -- check one tick either side
    lo_f, _ = F(u - 1.0)
    hi_f, _ = F(u + 1.0)
    quant = max(ONE, y_best * Decimal("1e-6"))
    flat = abs(lo_f) <= quant and abs(hi_f) <= quant
    return {"status": "NO_UNIQUE_ROOT" if flat else "OK", "u": u,
            "resid": resid, "unfilled_frac": unf, "monotone": mono,
            "root_ok": abs(resid) <= quant}


FIELDS = ["token", "tx", "block", "order_key", "router_sender", "router_family",
          "selected_pool", "selected_fee", "target_pool", "target_fee",
          "target_tick_spacing", "target_is_selected", "n_same_pair_hops_in_tx",
          "input_amount", "zero_for_one", "n_live_same_pair", "n_eligible_competitors",
          "target_active_liquidity", "target_actual_output", "target_unfilled_frac",
          "benchmark_pool", "benchmark_fee", "benchmark_output",
          "benchmark_impact_bps", "target_impact_bps", "routeability_margin_bps",
          "target_actual_spot", "benchmark_spot", "crossover_spot",
          "lowfee_ref_pool", "lowfee_ref_fee", "lowfee_ref_spot",
          "required_multiple_vs_benchmark", "actual_multiple_vs_benchmark",
          "required_multiple_vs_lowfee", "actual_multiple_vs_lowfee",
          "headroom_price_bps",
          "naive_multiple", "pairwise_fee_only_multiple", "no_target_slip_multiple",
          "eff_fee_effect", "competitor_slippage_effect", "target_slippage_effect",
          "status", "monotone", "root_ok", "root_residual_rel", "recon_consistency_rel"]
CFIELDS = ["token", "tx", "order_key", "router_family", "selected_pool", "selected_fee",
           "candidate_pool", "candidate_fee", "candidate_hooked", "candidate_output",
           "candidate_unfilled_frac", "candidate_eligible", "is_selected",
           "n_same_pair_hops_in_tx", "input_amount", "n_live_same_pair"]

fo = open(OUT, "w", newline="", encoding="utf-8")
fc = open(CANDOUT, "w", newline="", encoding="utf-8")
W = csv.DictWriter(fo, fieldnames=FIELDS); W.writeheader()
WC = csv.DictWriter(fc, fieldnames=CFIELDS); WC.writeheader()

n_orders = n_rows = n_inv_ok = n_inv_fail = 0
recon = []
status_ct = defaultdict(int)
mono_bad = root_bad = 0

for t in sorted(pools_for):
    ps = pools_for[t]
    if len(ps) < 2:
        continue
    timeline = []
    for p in ps:
        for e in cache.get(p["pool_id"], []):
            timeline.append((e["k"], p["pool_id"], e))
    timeline.sort(key=lambda x: x[0])
    live, last_fee, idx = {}, {}, {}

    def index_of(qid, net):
        """Tick index, rebuilt only when that pool's liquidity map changes."""
        if qid not in idx:
            idx[qid] = tick_index(net)
        return idx[qid]

    for okey, pid, e in timeline:
        p = meta[pid]
        if pid not in live:
            st = sr.PoolState(1, int(p["init_tick"]))
            st.sqrtP = sr.sqrt_at(int(p["init_tick"]))
            live[pid] = st
        st = live[pid]
        if e["t"] == "ML":
            st.apply_ml(e["lo"], e["hi"], e["dl"])
            idx.pop(pid, None)
            continue
        if e["sender"] in KNOWN_INFRA:
            fam = FAMILY[e["sender"]]
            zfo = e["a0"] < 0
            amt_in = abs(e["a0"]) if zfo else abs(e["a1"])
            actual_out = abs(e["a1"]) if zfo else abs(e["a0"])
            # GATE 2 (known answer): reproduce the selected pool's own execution
            ts_s, pref_s = index_of(pid, st.net)
            snap = FastState(1, st.tick).bind(st.net, ts_s, pref_s).seat(st.tick, st.sqrtP)
            sim_sel, _ = snap.swap_exact_in(zfo, amt_in, e["fee"], max_steps=MAX_STEPS)
            if not (actual_out > 0 and amt_in > 0 and
                    abs(sim_sel - Decimal(actual_out)) / Decimal(actual_out) <= Decimal("0.001")):
                n_inv_fail += 1
                st.sqrtP = Decimal(e["sp"]) / sr.Q96; st.tick = e["tick"]; last_fee[pid] = e["fee"]
                continue
            n_inv_ok += 1
            n_orders += 1

            # every live same-pair pool, simulated at the order's own size
            cands = []
            for qid, qst in live.items():
                q = meta[qid]
                if (q["token0"], q["token1"]) != (p["token0"], p["token1"]):
                    continue
                qts, qpref = index_of(qid, qst.net)
                s2 = FastState(1, qst.tick).bind(qst.net, qts, qpref).seat(qst.tick, qst.sqrtP)
                if s2.L <= 0:
                    continue
                qfee = e["fee"] if qid == pid else last_fee.get(qid, int(q["fee"]))
                y, _ = s2.swap_exact_in(zfo, amt_in, qfee, max_steps=MAX_STEPS)
                elig = (q["hooked"] == "False" and s2.unfilled <= 0 and
                        (qid == pid or used_before(qid, fam, okey)))
                cands.append({"pid": qid, "q": q, "fee": qfee, "y": y,
                              "unf": s2.unfilled_frac, "elig": elig,
                              "tick": qst.tick, "sqrtP": qst.sqrtP,
                              "net": qst.net, "ts": qts, "pref": qpref, "L": s2.L})

            nhops = len(tx_pair_pools.get((e["tx"], p["token0"], p["token1"]), {pid}))
            for c in cands:
                WC.writerow({"token": t, "tx": e["tx"], "order_key": "%d:%d:%d" % okey,
                             "router_family": fam, "selected_pool": pid,
                             "selected_fee": e["fee"], "candidate_pool": c["pid"],
                             "candidate_fee": c["fee"], "candidate_hooked": c["q"]["hooked"],
                             "candidate_output": g(c["y"]),
                             "candidate_unfilled_frac": g(c["unf"]),
                             "candidate_eligible": c["elig"], "is_selected": c["pid"] == pid,
                             "n_same_pair_hops_in_tx": nhops, "input_amount": str(amt_in),
                             "n_live_same_pair": len(cands)})

            for tg in cands:
                if tg["fee"] < EXTREME or tg["q"]["hooked"] != "False":
                    continue
                comp = [c for c in cands if c["pid"] != tg["pid"] and c["elig"]]
                if not comp:
                    continue
                bm = max(comp, key=lambda c: c["y"])
                y_best = bm["y"]
                if y_best <= 0:
                    continue
                lf = [c for c in comp if c["fee"] < LOWFEE]
                lfr = max(lf, key=lambda c: c["y"]) if lf else None

                fs = FastState(1, tg["tick"]).bind(tg["net"], tg["ts"], tg["pref"])
                x_act = x_of(tg["sqrtP"])
                fs.seat_x(x_act)
                y_recon, _ = fs.swap_exact_in(zfo, amt_in, tg["fee"], max_steps=MAX_STEPS)
                rc = (abs(y_recon - tg["y"]) / tg["y"]) if tg["y"] > 0 else Decimal(0)
                recon.append(float(rc))

                r = crossover(fs, zfo, amt_in, tg["fee"], y_best, x_act)
                status_ct[r["status"]] += 1
                if r["u"] is None:
                    xo = None
                    sp_x = None
                else:
                    xo = r["u"] if zfo else -r["u"]
                    sp_x = spot_out_per_in(sr.BASE ** (Decimal(xo) / 2), zfo)
                    if not r["monotone"]:
                        mono_bad += 1
                    if not r["root_ok"]:
                        root_bad += 1

                sp_act = spot_out_per_in(tg["sqrtP"], zfo)
                sp_bm = spot_out_per_in(bm["sqrtP"], zfo)
                sp_lf = spot_out_per_in(lfr["sqrtP"], zfo) if lfr else None
                ft = Decimal(tg["fee"]) / M
                fb = Decimal(bm["fee"]) / M
                naive = ONE / (ONE - ft)
                pair = (ONE - fb) / (ONE - ft)
                p_exec_b = y_best / Decimal(amt_in)
                nts = p_exec_b / ((ONE - ft) * sp_bm)
                req = (sp_x / sp_bm) if sp_x is not None else None
                # how much of each side's price the order itself consumed. A
                # benchmark with huge impact is a thin book, not a reference
                # price -- band medians are meaningless without stratifying here.
                p_exec_t = tg["y"] / Decimal(amt_in)
                bm_imp = (ONE - p_exec_b / (sp_bm * (ONE - fb))) * 10000
                tg_imp = (ONE - p_exec_t / (sp_act * (ONE - ft))) * 10000

                W.writerow({
                    "token": t, "tx": e["tx"], "block": okey[0],
                    "order_key": "%d:%d:%d" % okey, "router_sender": e["sender"],
                    "router_family": fam, "selected_pool": pid, "selected_fee": e["fee"],
                    "target_pool": tg["pid"], "target_fee": tg["fee"],
                    "target_tick_spacing": tg["q"]["tick_spacing"],
                    "target_is_selected": tg["pid"] == pid, "n_same_pair_hops_in_tx": nhops,
                    "input_amount": str(amt_in), "zero_for_one": zfo,
                    "n_live_same_pair": len(cands), "n_eligible_competitors": len(comp),
                    "target_active_liquidity": tg["L"],
                    "target_actual_output": g(tg["y"]),
                    "target_unfilled_frac": g(tg["unf"]),
                    "benchmark_pool": bm["pid"], "benchmark_fee": bm["fee"],
                    "benchmark_output": g(y_best),
                    "benchmark_impact_bps": g(bm_imp), "target_impact_bps": g(tg_imp),
                    "routeability_margin_bps": g((tg["y"] / y_best - ONE) * 10000),
                    "target_actual_spot": g(sp_act), "benchmark_spot": g(sp_bm),
                    "crossover_spot": g(sp_x),
                    "lowfee_ref_pool": lfr["pid"] if lfr else "",
                    "lowfee_ref_fee": lfr["fee"] if lfr else "",
                    "lowfee_ref_spot": g(sp_lf) if lfr else "",
                    "required_multiple_vs_benchmark": g(req),
                    "actual_multiple_vs_benchmark": g(sp_act / sp_bm),
                    "required_multiple_vs_lowfee": (g(sp_x / sp_lf)
                                                    if (sp_x is not None and lfr) else ""),
                    "actual_multiple_vs_lowfee": g(sp_act / sp_lf) if lfr else "",
                    "headroom_price_bps": (g((sp_act / sp_x - ONE) * 10000)
                                           if sp_x is not None and sp_x > 0 else ""),
                    "naive_multiple": g(naive),
                    "pairwise_fee_only_multiple": g(pair),
                    "no_target_slip_multiple": g(nts),
                    "eff_fee_effect": g(pair - naive),
                    "competitor_slippage_effect": g(nts - pair),
                    "target_slippage_effect": g(req - nts) if req is not None else "",
                    "status": r["status"], "monotone": r.get("monotone", ""),
                    "root_ok": r.get("root_ok", ""),
                    "root_residual_rel": (g(abs(r["resid"]) / y_best, 6)
                                          if r.get("resid") is not None else ""),
                    "recon_consistency_rel": g(rc, 6),
                })
                n_rows += 1
                if n_rows % 5000 == 0:
                    print(f"  rows {n_rows:,}  orders {n_orders:,}  token {t[:12]}")
        st.sqrtP = Decimal(e["sp"]) / sr.Q96
        st.tick = e["tick"]
        last_fee[pid] = e["fee"]

fo.close(); fc.close()

# ---------------------------------------------------------------- gates
print("\n" + "=" * 78)
print("ACCEPTANCE GATES")
print("=" * 78)
tot_inv = n_inv_ok + n_inv_fail
print(f"GATE 2 known-answer (selected pool reproduced within 10 bps): "
      f"{n_inv_ok:,}/{tot_inv:,} ({n_inv_ok/tot_inv*100:.2f}%)")
if tot_inv and n_inv_fail / tot_inv > 0.05:
    sys.exit("ABORT: >5% of orders failed the known-answer invariant")

if not n_rows:
    sys.exit("ABORT: zero crossover rows produced")
print(f"GATE 3 nonzero output: {n_rows:,} crossover rows over {n_orders:,} orders")

recon.sort()
rmed = recon[len(recon) // 2]
rmax = recon[-1]
r99 = recon[int(len(recon) * 0.99)]
print(f"GATE 4 state reconstruction (Y at rebuilt actual state vs direct sim):")
print(f"        median {rmed:.2e}   p99 {r99:.2e}   max {rmax:.2e}")
if rmed > 1e-9 or r99 > 1e-3:
    sys.exit("ABORT: log-coordinate state reconstruction is not faithful")

ok = status_ct.get("OK", 0)
print(f"GATE 5 root status: " + "  ".join(f"{k}={v:,}" for k, v in sorted(status_ct.items())))
print(f"        OK share {ok/n_rows*100:.2f}%")
if ok / n_rows < 0.90:
    sys.exit("ABORT: >10% of rows produced no usable crossover")
print(f"GATE 6 monotonicity violations: {mono_bad:,} ({mono_bad/max(1,ok)*100:.2f}% of OK)")
print(f"GATE 7 root residual failures:  {root_bad:,} ({root_bad/max(1,ok)*100:.2f}% of OK)")
if mono_bad / max(1, ok) > 0.01 or root_bad / max(1, ok) > 0.01:
    sys.exit("ABORT: bracket monotonicity or root residual above the 1% ceiling")
print("\nall gates passed")

report()
