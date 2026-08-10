"""Decompose the same-pair selection-regret tail and test alternative eligibility.

The headline result is that routers pick the best simultaneously available
same-pair pool 97.8% of the time. This asks what the other 2.2% have in common,
and specifically whether the better alternative was plausibly SELECTABLE.

The decisive test: had the same router family ever executed against that
alternative pool -- and had it done so BEFORE this order? If yes, "the router
does not support that pool" is largely eliminated and the explanation moves
toward indexing latency, stale quotes or momentary source restrictions. If the
alternative is never touched by that router anywhere, eligibility/filtering
becomes the leading explanation.

Runs entirely from the cached event history. No new RPC.
"""
import csv, json, os, statistics, sys
from collections import Counter, defaultdict
from decimal import Decimal

QUOTES = "data/analysis/HISTORICAL_QUOTE_QUALITY.csv"
EVCACHE = "data/analysis/_quote_events_cache.json"
CENSUS = "data/analysis/CHAINWIDE_POOL_CENSUS.csv"
OUT = "data/analysis/REGRET_TAIL_ATTRIBUTION.csv"
SEVERE = "data/analysis/REGRET_SEVERE_MISSES.csv"

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

cache = json.load(open(EVCACHE))
for ev in cache.values():
    for e in ev:
        if isinstance(e.get("k"), list):
            e["k"] = tuple(e["k"])
meta = {}
for r in csv.DictReader(open(CENSUS)):
    meta[r["pool_id"]] = r

# tx+pool -> sender, and per-pool index of (sender -> sorted keys) for eligibility
sender_of = {}
pool_router_keys = defaultdict(lambda: defaultdict(list))
for pid, ev in cache.items():
    for e in ev:
        if e["t"] != "SWAP":
            continue
        sender_of[(e["tx"], pid)] = e["sender"]
        pool_router_keys[pid][e["sender"]].append(e["k"])
for pid in pool_router_keys:
    for s in pool_router_keys[pid]:
        pool_router_keys[pid][s].sort()

rows = list(csv.DictReader(open(QUOTES)))
out = []
for r in rows:
    if not r["selection_regret_bps"]:
        continue
    reg = Decimal(r["selection_regret_bps"])
    sender = sender_of.get((r["tx"], r["selected_pool"]), "")
    fam = FAMILY.get(sender, "other")
    alt = r["best_competing_pool"]
    am = meta.get(alt, {})
    sm = meta.get(r["selected_pool"], {})
    keys = pool_router_keys.get(alt, {})
    ever_same = bool(keys.get(sender))
    fam_senders = [s for s in keys if FAMILY.get(s, "other") == fam and fam != "other"]
    ever_family = bool(fam_senders)
    order_key = None
    for e in cache.get(r["selected_pool"], []):
        if e["t"] == "SWAP" and e["tx"] == r["tx"]:
            order_key = e["k"]; break
    before_same = bool(order_key and any(k < order_key for k in keys.get(sender, [])))
    before_family = bool(order_key and any(k < order_key
                                           for s in fam_senders for k in keys[s]))
    out.append({
        "tx": r["tx"], "token": r["token"], "block": r["block"],
        "router_sender": sender, "router_family": fam,
        "selected_pool": r["selected_pool"], "selected_fee": int(r["selected_fee"]),
        "selected_hooked": sm.get("hooked", ""), "selected_output": r["selected_output"],
        "alt_pool": alt, "alt_fee": r["best_competing_fee"],
        "alt_hooked": am.get("hooked", ""), "alt_tick_spacing": am.get("tick_spacing", ""),
        "alt_init_block": am.get("init_block", ""),
        "alt_age_blocks": (int(r["block"]) - int(am["init_block"])) if am.get("init_block") else "",
        "alt_output": r["best_competing_output"],
        "regret_bps": str(reg), "beaten": reg < 0,
        "shortfall_bps": str(-reg) if reg < 0 else "",
        "alt_ever_used_by_same_sender": ever_same,
        "alt_ever_used_by_same_family": ever_family,
        "alt_used_before_this_order_same_sender": before_same,
        "alt_used_before_this_order_same_family": before_family,
    })

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)

beaten = [o for o in out if o["beaten"]]
severe = [o for o in beaten if Decimal(o["shortfall_bps"]) > 1000]
with open(SEVERE, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(severe)

print("=" * 78)
print("REGRET TAIL ATTRIBUTION")
print("=" * 78)
print(f"  orders with an alternative: {len(out):,}")
print(f"  beaten: {len(beaten):,}   severe (>1000 bps): {len(severe):,}")

sh = sorted(float(o["shortfall_bps"]) for o in beaten)
print(f"\n  SHORTFALL MAGNITUDE (positive bps, larger = worse)")
print(f"    median shortfall       {statistics.median(sh):8.1f} bps")
print(f"    75th pct shortfall     {sh[int(len(sh)*0.75)]:8.1f} bps")
print(f"    90th pct shortfall     {sh[int(len(sh)*0.90)]:8.1f} bps")
print(f"    max shortfall          {sh[-1]:8.1f} bps")

print("\n  BY ROUTER FAMILY")
print(f"    {'family':26} {'orders':>8} {'beaten':>8} {'beaten%':>9} {'severe':>8} {'severe%':>9}")
for fam in sorted({o["router_family"] for o in out}):
    g = [o for o in out if o["router_family"] == fam]
    b = [o for o in g if o["beaten"]]
    s = [o for o in b if Decimal(o["shortfall_bps"]) > 1000]
    print(f"    {fam:26} {len(g):>8,} {len(b):>8,} {len(b)/len(g)*100:>8.2f}% "
          f"{len(s):>8,} {len(s)/len(g)*100:>8.2f}%")

print("\n  WAS THE BETTER ALTERNATIVE PLAUSIBLY SELECTABLE?")
for label, grp in (("all beaten", beaten), ("severe (>1000bps)", severe)):
    if not grp: continue
    es = sum(1 for o in grp if o["alt_ever_used_by_same_sender"])
    ef = sum(1 for o in grp if o["alt_ever_used_by_same_family"])
    bs = sum(1 for o in grp if o["alt_used_before_this_order_same_sender"])
    bf = sum(1 for o in grp if o["alt_used_before_this_order_same_family"])
    print(f"    -- {label} (n={len(grp):,}) --")
    print(f"       alt ever used by the SAME SENDER:   {es:>6,} ({es/len(grp)*100:5.1f}%)")
    print(f"       alt ever used by the SAME FAMILY:   {ef:>6,} ({ef/len(grp)*100:5.1f}%)")
    print(f"       alt used BEFORE this order (sender):{bs:>6,} ({bs/len(grp)*100:5.1f}%)")
    print(f"       alt used BEFORE this order (family):{bf:>6,} ({bf/len(grp)*100:5.1f}%)")

print("\n  SEVERE MISSES: concentration by best-alternative pool")
cc = Counter(o["alt_pool"] for o in severe)
print(f"    distinct alternative pools: {len(cc):,} across {len(severe):,} severe misses")
top = cc.most_common(10)
cov = sum(n for _, n in top)
print(f"    top 10 alternatives cover {cov}/{len(severe)} ({cov/len(severe)*100:.1f}%)")
for pid, n in top:
    m = meta.get(pid, {})
    ex = [o for o in severe if o["alt_pool"] == pid]
    ever = sum(1 for o in ex if o["alt_ever_used_by_same_family"])
    print(f"      {pid[:20]}… n={n:>4} fee={int(m.get('fee',0))/10000:>7.2f}% "
          f"hooked={m.get('hooked','?'):5} family-used-before={ever}/{n}")

print("\n  SEVERE MISSES: alternative characteristics")
hk = Counter(o["alt_hooked"] for o in severe)
print(f"    alt hooked: {dict(hk)}")
ages = [int(o["alt_age_blocks"]) for o in severe if o["alt_age_blocks"] != ""]
if ages:
    ages.sort()
    print(f"    alt age at order (blocks): median {ages[len(ages)//2]:,}, "
          f"p10 {ages[len(ages)//10]:,}, min {ages[0]:,}")
fb = Counter(("<10%" if int(o["alt_fee"]) < 100000 else
              "10-40%" if int(o["alt_fee"]) < 400000 else
              "40-70%" if int(o["alt_fee"]) < 700000 else
              "70-90%" if int(o["alt_fee"]) < 900000 else "90%+") for o in severe)
print(f"    alt fee band: {dict(fb)}")
