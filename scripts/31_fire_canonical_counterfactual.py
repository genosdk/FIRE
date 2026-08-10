"""Exact reconstruction of the omitted FIRE -> ETH -> USDG route for the 20 orders.

Uses the user's TOTAL FIRE input per order (routes split FIRE across several
FIRE/USDG pools; the counterfactual asks what that same total would have made
through the canonical path instead).

State source: each v4 Swap event reports the pool's resulting sqrtPriceX96 AND
its active liquidity, so the last swap strictly before the target
(block, txIndex, logIndex) gives exact state without replaying full history --
which matters because the deep ETH/USDG venue has ~190k swaps.

That shortcut is validated, not assumed: for held-out historical swaps in each
pool we predict the output from the PREVIOUS swap's reported (sqrtP, L) and
compare to what actually happened. Trades here are small (~0.002-0.02 ETH) so
within-range execution is expected, and the validation shows whether that holds.

ETH and WETH are treated as 1:1 economically; the asset transition is preserved
in the output so the wrapping step is explicit.
"""
import csv, json, os, statistics, subprocess, sys, time
from collections import defaultdict
from decimal import Decimal, getcontext
sys.path.insert(0, "scripts")
from importlib import import_module
sr = import_module("27_state_replay")
getcontext().prec = 60

PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
CANON = "0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
HOPS = "data/analysis/FIRE_EXACT_HOP_REPLAY.csv"
ORDERS = "data/analysis/FIRE_RELAY_ORDERS.csv"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
OUT = "data/analysis/FIRE_CANONICAL_COUNTERFACTUAL.csv"
WIN = 250_000


def key(L):
    return (int(L["blockNumber"], 16), int(L["transactionIndex"], 16), int(L["logIndex"], 16))


def swaps_in(pool, lo, hi):
    out = []
    for L in sr.logs_adaptive([SWAP, pool], max(0, lo), hi):
        d = L["data"][2:]
        w = [d[i:i + 64] for i in range(0, len(d), 64)]
        out.append({"k": key(L), "sp": int(w[2], 16), "L": int(w[3], 16),
                    "a0": sr.i128(w[0]), "a1": sr.i128(w[1]),
                    "fee": int(w[5], 16) & 0xFFFFFF,
                    "sender": ("0x" + L["topics"][2][26:]).lower()})
    out.sort(key=lambda x: x["k"])
    return out


def sim_in_range(sqrtP, L, zero_for_one, amount_in, fee_pips):
    """Within-range exact-input swap. Returns (out, sqrtP_after)."""
    if L <= 0:
        return Decimal(0), sqrtP
    rem = Decimal(amount_in) * (Decimal(1_000_000) - Decimal(fee_pips)) / Decimal(1_000_000)
    Ld = Decimal(L)
    a = sqrtP
    if zero_for_one:
        b = Decimal(1) / (Decimal(1) / a + rem / Ld)
        return Ld * (a - b), b
    b = a + rem / Ld
    return Ld * (Decimal(1) / a - Decimal(1) / b), b


# ---- candidate ETH/USDG bridges ----------------------------------------
bridges = []
for r in csv.DictReader(open(CENSUS)):
    if {r["token0"].lower(), r["token1"].lower()} == {NATIVE, USDG} and r["fee"] != "8388608":
        if int(r["fee"]) <= 10000:
            bridges.append(r)
print(f"ETH/USDG v4 bridge candidates (fee<=1%): {len(bridges)}")

hops = list(csv.DictReader(open(HOPS)))
orders = defaultdict(list)
for h in hops:
    orders[h["order_tx"]].append(h)
odata = {r["origin_tx"].lower(): r for r in csv.DictReader(open(ORDERS))
         if r["relay_depositor"] == r["fire_source"]}
print(f"orders to reconstruct: {len(orders)}")

# ---- canonical + bridge state at each order, from windowed swap history --
print("\nfetching state windows...")
canon_state, bridge_state = {}, defaultdict(dict)
canon_hist, bridge_hist = {}, defaultdict(dict)
for i, (tx, hs) in enumerate(sorted(orders.items()), 1):
    kk = min((int(h["block"]), int(h["tx_index"]), int(h["log_index"])) for h in hs)
    b = kk[0]
    cs = swaps_in(CANON, b - WIN, b)
    prev = [s for s in cs if s["k"] < kk]
    canon_state[tx] = prev[-1] if prev else None
    canon_hist[tx] = cs
    for br in bridges:
        bs = swaps_in(br["pool_id"], b - WIN, b)
        p = [s for s in bs if s["k"] < kk]
        if p:
            bridge_state[tx][br["pool_id"]] = p[-1]
            bridge_hist[tx][br["pool_id"]] = bs
    time.sleep(0.5)
    print(f"  {i}/{len(orders)}  canonical_obs={len(cs)}  bridges_with_state={len(bridge_state[tx])}")

# ---- validate the (sqrtP, L)-from-previous-swap shortcut -----------------
print("\n" + "=" * 74)
print("INVARIANT: predict each swap from the PREVIOUS swap's reported state")
print("=" * 74)
def validate(hist, label):
    ok = bad = 0
    errs = []
    for j in range(1, len(hist)):
        p, c = hist[j - 1], hist[j]
        zfo = c["a0"] < 0
        ain = abs(c["a0"]) if zfo else abs(c["a1"])
        aout = abs(c["a1"]) if zfo else abs(c["a0"])
        if aout <= 0:
            continue
        o, _ = sim_in_range(Decimal(p["sp"]) / sr.Q96, p["L"], zfo, ain, c["fee"])
        rel = abs(o - Decimal(aout)) / Decimal(aout)
        if rel < Decimal("0.005"):
            ok += 1
        else:
            bad += 1
            if len(errs) < 2:
                errs.append(f"{rel*100:.1f}%")
    return ok, bad, errs

allc = []
seen = set()
for tx, cs in canon_hist.items():
    for s in cs:
        if s["k"] not in seen:
            seen.add(s["k"]); allc.append(s)
allc.sort(key=lambda x: x["k"])
ok, bad, errs = validate(allc, "canonical")
print(f"  canonical FIRE/ETH: {ok}/{ok+bad} within 0.5%" + (f"  worst {errs}" if errs else ""))
canon_rate = ok / max(1, ok + bad)
for br in bridges[:3]:
    hs = []
    seen = set()
    for tx in bridge_hist:
        for s in bridge_hist[tx].get(br["pool_id"], []):
            if s["k"] not in seen:
                seen.add(s["k"]); hs.append(s)
    hs.sort(key=lambda x: x["k"])
    if len(hs) > 5:
        o2, b2, e2 = validate(hs, "bridge")
        print(f"  bridge fee={int(br['fee'])/10000:>6.3f}%: {o2}/{o2+b2} within 0.5%")

# ---- counterfactual ------------------------------------------------------
rows = []
for tx, hs in sorted(orders.items()):
    od = odata.get(tx)
    if not od:
        continue
    fire_total = sum(Decimal(h["fire_in"]) for h in hs if h["fire_in"])
    actual = Decimal(od["deposit_amount_raw"]) / Decimal(10 ** 6)
    cs = canon_state.get(tx)
    if not cs or fire_total <= 0:
        rows.append({"tx": tx, "fire_input": str(fire_total), "actual_usdg": str(actual),
                     "canonical_usdg": "", "status": "NO_CANONICAL_STATE"})
        continue
    # canonical pool: currency0 = native ETH, currency1 = FIRE -> selling FIRE is oneForZero
    eth_out, _ = sim_in_range(Decimal(cs["sp"]) / sr.Q96, cs["L"], False,
                              int(fire_total * Decimal(10 ** 18)), cs["fee"])
    best = None
    for pid, st in bridge_state[tx].items():
        m = next(b for b in bridges if b["pool_id"] == pid)
        # ETH/USDG: currency0 = native ETH -> selling ETH is zeroForOne
        u, _ = sim_in_range(Decimal(st["sp"]) / sr.Q96, st["L"], True,
                            int(eth_out), int(m["fee"]))
        if u > 0 and (best is None or u > best[0]):
            best = (u, pid, int(m["fee"]))
    if not best:
        rows.append({"tx": tx, "fire_input": str(fire_total), "actual_usdg": str(actual),
                     "canonical_usdg": "", "status": "NO_BRIDGE_STATE"})
        continue
    cf = best[0] / Decimal(10 ** 6)
    sender = hs[0]["router_sender"]
    canon_used = any(s["sender"] == sender for s in canon_hist[tx])
    canon_before = any(s["sender"] == sender and s["k"] < (int(hs[0]["block"]), int(hs[0]["tx_index"]), int(hs[0]["log_index"]))
                       for s in canon_hist[tx])
    br_used = any(s["sender"] == sender for s in bridge_hist[tx].get(best[1], []))
    rows.append({
        "tx": tx, "router_sender": sender, "router_family": hs[0]["router_family"],
        "fire_input": str(fire_total), "actual_usdg": str(actual),
        "canonical_eth_out": str(Decimal(eth_out) / Decimal(10 ** 18)),
        "bridge_pool": best[1], "bridge_fee": best[2],
        "canonical_usdg": str(cf),
        "actual_vs_canonical_pct": str((actual / cf - 1) * 100) if cf > 0 else "",
        "usdg_shortfall": str(cf - actual),
        "canonical_used_by_sender": canon_used,
        "canonical_used_before_order": canon_before,
        "bridge_used_by_sender": br_used,
        "status": "OK"})

with open(OUT, "w", newline="", encoding="utf-8") as f:
    ks = sorted({k for r in rows for k in r})
    w = csv.DictWriter(f, fieldnames=ks); w.writeheader(); w.writerows(rows)

good = [r for r in rows if r.get("status") == "OK"]
print("\n" + "=" * 92)
print("FIRE -> ETH -> USDG COUNTERFACTUAL (exact state)")
print("=" * 92)
print(f"  {'tx':14} {'FIRE in':>12} {'actual':>10} {'canonical':>11} {'diff %':>9} {'short USDG':>11}")
for r in sorted(good, key=lambda x: float(x["actual_vs_canonical_pct"])):
    print(f"  {r['tx'][:12]}… {Decimal(r['fire_input']):>12,.0f} {Decimal(r['actual_usdg']):>10.4f} "
          f"{Decimal(r['canonical_usdg']):>11.4f} {Decimal(r['actual_vs_canonical_pct']):>+8.2f}% "
          f"{Decimal(r['usdg_shortfall']):>11.4f}")
if good:
    d = sorted(float(r["actual_vs_canonical_pct"]) for r in good)
    ta = sum(Decimal(r["actual_usdg"]) for r in good)
    tc = sum(Decimal(r["canonical_usdg"]) for r in good)
    worse = sum(1 for x in d if x < 0)
    print(f"\n  orders reconstructed:        {len(good)}/{len(rows)}")
    print(f"  canonical route better:      {worse}/{len(good)}")
    print(f"  median difference:           {statistics.median(d):+.2f}%")
    print(f"  best / worst:                {max(d):+.2f}% / {min(d):+.2f}%")
    print(f"  total actual USDG:           {ta:.4f}")
    print(f"  total canonical USDG:        {tc:.4f}")
    print(f"  total left on the table:     {tc - ta:.4f} USDG")
    print(f"  VOLUME-WEIGHTED difference:  {(ta/tc-1)*100:+.2f}%")
    print(f"\n  earlier observational estimate: -8.59% to -9.13% volume-weighted")
    cu = sum(1 for r in good if r["canonical_used_by_sender"] in (True, "True"))
    cb = sum(1 for r in good if r["canonical_used_before_order"] in (True, "True"))
    bu = sum(1 for r in good if r["bridge_used_by_sender"] in (True, "True"))
    print(f"\n  canonical FIRE/ETH ever used by that sender:  {cu}/{len(good)}")
    print(f"  canonical used BEFORE the order:              {cb}/{len(good)}")
    print(f"  chosen bridge ever used by that sender:       {bu}/{len(good)}")
