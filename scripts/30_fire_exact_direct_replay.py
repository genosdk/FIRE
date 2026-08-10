"""Forensic exact-state replay of the 20 Relay FIRE-origin orders.

Hop-level, not collapsed: each FIRE/USDG swap inside each order is compared
against every simultaneously live FIRE/USDG pool at that exact
(block, txIndex, logIndex).

Counterfactual confidence is split, per the tail analysis:
  HOOKLESS alternative -- high confidence. State/math fully reconstructed.
  HOOKED alternative   -- candidate only. Reproducing a hooked pool's actual
                          history proves the replay matches what happened; it
                          does not prove a counterfactual quote is valid, since
                          a hook may condition on caller, calldata, time or
                          returned deltas.
The FIRE 40% and 50% pools are themselves hookless, so selected-side execution
is high confidence throughout.
"""
import csv, json, os, sys, time
from collections import defaultdict
from decimal import Decimal
sys.path.insert(0, "scripts")
from importlib import import_module
sr = import_module("27_state_replay")

FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
ORDERS = "data/analysis/FIRE_RELAY_ORDERS.csv"
EVCACHE = "data/analysis/_quote_events_cache.json"
OUT_HOP = "data/analysis/FIRE_EXACT_HOP_REPLAY.csv"
OUT_ORD = "data/analysis/FIRE_EXACT_ORDER_CLASS.csv"
FAMILY = {
    "0x1d4b86491ec211257cbedd77a4380a7494624eff": "0x Settler",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7": "0x Settler",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f": "0x Settler",
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap UniversalRouter",
    "0x8f10b468b06c6fd214b65f87778827f7d113f996": "unlabelled router B",
    "0xa633c73658857d5241fe21d43f854a82e2d0592c": "unlabelled router A",
}

pools = [r for r in csv.DictReader(open(CENSUS))
         if {r["token0"].lower(), r["token1"].lower()} == {FIRE, USDG}]
print(f"FIRE/USDG pools on-chain: {len(pools)}")
for p in sorted(pools, key=lambda x: int(x["fee"])):
    print(f"  {p['pool_id'][:20]}…  fee={int(p['fee'])/10000:>8.3f}%  hooked={p['hooked']}")

cache = json.load(open(EVCACHE)) if os.path.exists(EVCACHE) else {}
for ev in cache.values():
    for e in ev:
        if isinstance(e.get("k"), list):
            e["k"] = tuple(e["k"])
latest = int(sr.rpc("eth_blockNumber", []), 16)
missing = [p for p in pools if p["pool_id"] not in cache]
print(f"\nfetching events for {len(missing)} uncached FIRE/USDG pools")
for i, p in enumerate(missing, 1):
    cache[p["pool_id"]] = sr.fetch_pool_events(p["pool_id"], int(p["init_block"]), latest)
    time.sleep(1.0)
    print(f"  {i}/{len(missing)}")
if missing:
    json.dump({k: [{**e, "k": list(e["k"])} for e in v] for k, v in cache.items()},
              open(EVCACHE, "w"))

orders = [r for r in csv.DictReader(open(ORDERS)) if r["relay_depositor"] == r["fire_source"]]
order_tx = {r["origin_tx"].lower(): r for r in orders}
print(f"\nRelay FIRE orders: {len(order_tx)}")

meta = {p["pool_id"]: p for p in pools}
timeline = []
for p in pools:
    for e in cache.get(p["pool_id"], []):
        timeline.append((e["k"], p["pool_id"], e))
timeline.sort(key=lambda x: x[0])

# prior-use index per pool per sender
prior = defaultdict(lambda: defaultdict(list))
for k, pid, e in timeline:
    if e["t"] == "SWAP":
        prior[pid][e["sender"]].append(k)

live, hops = {}, []
for k, pid, e in timeline:
    p = meta[pid]
    if pid not in live:
        st = sr.PoolState(1, int(p["init_tick"]))
        st.sqrtP = sr.sqrt_at(int(p["init_tick"]))
        live[pid] = st
    st = live[pid]
    if e["t"] == "ML":
        st.apply_ml(e["lo"], e["hi"], e["dl"])
        continue
    tx = e["tx"].lower()
    if tx in order_tx:
        zfo = e["a0"] < 0
        amt_in = abs(e["a0"]) if zfo else abs(e["a1"])
        actual_out = abs(e["a1"]) if zfo else abs(e["a0"])
        snap = sr.PoolState(1, st.tick); snap.sqrtP = st.sqrtP
        snap.net = dict(st.net); snap.recompute_active()
        sim_sel, _ = snap.swap_exact_in(zfo, amt_in, e["fee"])
        inv_ok = actual_out > 0 and abs(sim_sel - Decimal(actual_out)) / Decimal(actual_out) < Decimal("0.001")
        alts = []
        for qid, qst in live.items():
            if qid == pid:
                continue
            q = meta[qid]
            s2 = sr.PoolState(1, qst.tick); s2.sqrtP = qst.sqrtP
            s2.net = dict(qst.net); s2.recompute_active()
            if s2.L <= 0:
                continue
            o, _ = s2.swap_exact_in(zfo, amt_in, int(q["fee"]))
            alts.append({"pool": qid, "fee": int(q["fee"]),
                         "hooked": q["hooked"] == "True", "out": o})
        sel = Decimal(actual_out)
        best = max(alts, key=lambda a: a["out"], default=None)
        bh = max([a for a in alts if not a["hooked"]], key=lambda a: a["out"], default=None)
        cheap = max([a for a in alts if a["fee"] < 10000], key=lambda a: a["out"], default=None)
        def bps(a): return (sel / a["out"] - 1) * 10000 if a and a["out"] > 0 else None
        pu = prior[best["pool"]][e["sender"]] if best else []
        before = [x for x in pu if x < k]
        hops.append({
            "order_tx": tx, "block": k[0], "tx_index": k[1], "log_index": k[2],
            "router_sender": e["sender"], "router_family": FAMILY.get(e["sender"], "other"),
            "selected_pool": pid, "selected_fee": e["fee"], "selected_hooked": p["hooked"],
            "fire_in": str(Decimal(amt_in) / Decimal(10**18)) if zfo else "",
            "selected_output": str(sel), "invariant_ok": inv_ok,
            "n_alternatives": len(alts),
            "best_pool": best["pool"] if best else "", "best_fee": best["fee"] if best else "",
            "best_hooked": best["hooked"] if best else "",
            "best_output": str(best["out"]) if best else "",
            "direct_pool_regret_bps": str(bps(best)) if best else "",
            "best_hookless_pool": bh["pool"] if bh else "",
            "best_hookless_fee": bh["fee"] if bh else "",
            "hookless_regret_bps": str(bps(bh)) if bh else "",
            "cheap_alt_fee": cheap["fee"] if cheap else "",
            "cheap_alt_regret_bps": str(bps(cheap)) if cheap else "",
            "alt_ever_used_by_sender": bool(pu),
            "alt_used_before_this_order": bool(before),
            "blocks_since_prior_use": (k[0] - before[-1][0]) if before else "",
        })
    st.sqrtP = Decimal(e["sp"]) / sr.Q96
    st.tick = e["tick"]

if not hops:
    sys.exit("no FIRE hops matched")
with open(OUT_HOP, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(hops[0].keys())); w.writeheader(); w.writerows(hops)

print("\n" + "=" * 78)
print("FIRE ORDERS -- HOP-LEVEL DIRECT SELECTION")
print("=" * 78)
print(f"  hops reconstructed: {len(hops)}  across {len({h['order_tx'] for h in hops})} orders")
print(f"  invariant passed:   {sum(1 for h in hops if h['invariant_ok'])}/{len(hops)}")
print(f"\n  {'order':14} {'fee':>8} {'regret bps':>12} {'hookless bps':>14} {'best alt fee':>13} {'hooked?':>8}")
for h in sorted(hops, key=lambda x: (x["order_tx"], x["block"])):
    r = h["direct_pool_regret_bps"]; hl = h["hookless_regret_bps"]
    print(f"  {h['order_tx'][:12]}… {int(h['selected_fee'])/10000:>7.2f}% "
          f"{(float(r) if r else 0):>12.1f} {(float(hl) if hl else 0):>14.1f} "
          f"{(int(h['best_fee'])/10000 if h['best_fee'] else 0):>12.2f}% {str(h['best_hooked']):>8}")

def cls(v):
    if v is None: return "NO_ALTERNATIVE"
    if v >= 0: return "DIRECT_OPTIMAL"
    if v >= -100: return "DIRECT_NEAR_OPTIMAL"
    if v >= -1000: return "DIRECT_SUBOPTIMAL"
    return "DIRECT_SEVERE"

orows = []
for tx in sorted({h["order_tx"] for h in hops}):
    hs = [h for h in hops if h["order_tx"] == tx]
    worst = min((float(h["direct_pool_regret_bps"]) for h in hs
                 if h["direct_pool_regret_bps"]), default=None)
    worst_hl = min((float(h["hookless_regret_bps"]) for h in hs
                    if h["hookless_regret_bps"]), default=None)
    any_hookless_better = any(h["hookless_regret_bps"] and float(h["hookless_regret_bps"]) < -100
                              for h in hs)
    any_better = any(h["direct_pool_regret_bps"] and float(h["direct_pool_regret_bps"]) < -100
                     for h in hs)
    alt_kind = ("BETTER_HOOKLESS_ALT" if any_hookless_better else
                "BETTER_HOOKED_ALT_ONLY" if any_better else "NO_BETTER_ALT")
    orows.append({"order_tx": tx, "hops": len(hs),
                  "worst_direct_regret_bps": worst if worst is not None else "",
                  "worst_hookless_regret_bps": worst_hl if worst_hl is not None else "",
                  "classification": cls(worst), "alt_kind": alt_kind,
                  "routers": "|".join(sorted({h["router_family"] for h in hs}))})
with open(OUT_ORD, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(orows[0].keys())); w.writeheader(); w.writerows(orows)

from collections import Counter
print("\n  ORDER-LEVEL CLASSIFICATION")
for k, v in Counter(o["classification"] for o in orows).most_common():
    print(f"    {k:22} {v:>3}/{len(orows)}")
print("\n  ALTERNATIVE KIND (high-confidence hookless vs hooked-only)")
for k, v in Counter(o["alt_kind"] for o in orows).most_common():
    print(f"    {k:24} {v:>3}/{len(orows)}")
ev = sum(1 for h in hops if h["alt_ever_used_by_sender"])
bf = sum(1 for h in hops if h["alt_used_before_this_order"])
print(f"\n  best alternative ever used by that sender: {ev}/{len(hops)}")
print(f"  best alternative used BEFORE this order:   {bf}/{len(hops)}")
