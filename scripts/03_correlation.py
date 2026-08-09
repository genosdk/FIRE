import csv
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, getcontext

getcontext().prec = 50

LIQ_FILE = "FIRE_USDG_ALL_modify_liquidity.csv"
SWAP_FILE = "FIRE_USDG_ALL_swaps.csv"

OUT_TIMELINE = "FIRE_USDG_CORRELATED_timeline.csv"
OUT_SWAPS = "FIRE_USDG_CORRELATED_swaps.csv"
OUT_ZERO = "FIRE_USDG_ZERO_candidates.csv"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dec(value):
    if value in ("", None):
        return Decimal(0)
    return Decimal(str(value))


def intval(value):
    if value in ("", None):
        return 0
    return int(value)


def short(addr):
    if not addr:
        return ""
    return addr[:8] + "…" + addr[-6:]


liq = read_csv(LIQ_FILE)
swaps = read_csv(SWAP_FILE)

for r in liq:
    r["_time"] = dt(r["timestamp_utc"])
    r["_block"] = intval(r["block"])
    r["_txi"] = intval(r["transaction_index"])
    r["_logi"] = intval(r["log_index"])
    r["_liq_delta"] = intval(r["liquidity_delta"])

for r in swaps:
    r["_time"] = dt(r["timestamp_utc"])
    r["_block"] = intval(r["block"])
    r["_txi"] = intval(r["transaction_index"])
    r["_logi"] = intval(r["log_index"])
    r["_fee_raw"] = intval(r["fee_raw"])

timeline = []
for r in swaps:
    x = dict(r); x["kind"] = "SWAP"; x["_sort"] = (r["_block"], r["_txi"], r["_logi"]); timeline.append(x)
for r in liq:
    x = dict(r); x["kind"] = "LIQUIDITY"; x["_sort"] = (r["_block"], r["_txi"], r["_logi"]); timeline.append(x)
timeline.sort(key=lambda x: x["_sort"])

liq_by_pool = defaultdict(list)
for r in liq:
    liq_by_pool[r["pool"]].append(r)
for pool in liq_by_pool:
    liq_by_pool[pool].sort(key=lambda r: (r["_block"], r["_txi"], r["_logi"]))

zero_by_pool = defaultdict(list)
for r in liq:
    if r["action"] == "ZERO":
        zero_by_pool[r["pool"]].append(r)

LP_FEE = {"FIRE_USDG_50pct": Decimal("500000"), "FIRE_USDG_40pct": Decimal("400000")}


def infer_protocol_fee(combined, lp_fee):
    combined = Decimal(combined); lp_fee = Decimal(lp_fee)
    denom = Decimal(1) - lp_fee / Decimal(1_000_000)
    if denom == 0:
        return Decimal(0)
    p = (combined - lp_fee) / denom
    return p.quantize(Decimal("1"))


def lp_effective_fee_fraction(combined_fee, nominal_lp_fee):
    p = infer_protocol_fee(combined_fee, nominal_lp_fee)
    lp_component_pips = Decimal(nominal_lp_fee) * (Decimal(1) - p / Decimal(1_000_000))
    return (lp_component_pips / Decimal(1_000_000), p)


correlated = []
for s in swaps:
    pool = s["pool"]; st = s["_time"]; sb = s["_block"]
    later = []
    for l in liq_by_pool[pool]:
        if (l["_block"], l["_txi"], l["_logi"]) <= (s["_block"], s["_txi"], s["_logi"]):
            continue
        seconds = (l["_time"] - st).total_seconds()
        if seconds < 0:
            continue
        later.append((seconds, l))

    nearest = later[0] if later else None
    zero_later = [(seconds, l) for seconds, l in later if l["action"] == "ZERO"]
    nearest_zero = zero_later[0] if zero_later else None

    zero_same_block = False; zero_1min = False; zero_5min = False; zero_1hr = False
    nz_seconds = None; nz_blocks = None; nz_owner = ""; nz_nft = ""; nz_tx = ""

    if nearest_zero:
        nz_seconds, z = nearest_zero
        nz_blocks = z["_block"] - sb
        zero_same_block = (z["_block"] == sb)
        zero_1min = nz_seconds <= 60
        zero_5min = nz_seconds <= 300
        zero_1hr = nz_seconds <= 3600
        nz_owner = z["tx_from"]; nz_nft = z["possible_token_id"]; nz_tx = z["transaction_hash"]

    nominal = LP_FEE[pool]
    lp_frac, protocol_pips = lp_effective_fee_fraction(s["_fee_raw"], nominal)
    direction = s["direction"]
    fire_fee_est = Decimal(0); usdg_fee_est = Decimal(0)
    if direction == "FIRE_TO_USDG":
        fire_fee_est = dec(s["fire_in"]) * lp_frac
    elif direction == "USDG_TO_FIRE":
        usdg_fee_est = dec(s["usdg_in"]) * lp_frac

    correlated.append({
        "pool": pool,
        "swap_timestamp": s["timestamp_utc"],
        "swap_block": s["block"],
        "swap_tx": s["transaction_hash"],
        "trader_tx_from": s["tx_from"],
        "swap_sender": s["swap_sender"],
        "direction": direction,
        "fire_in": s["fire_in"], "fire_out": s["fire_out"],
        "usdg_in": s["usdg_in"], "usdg_out": s["usdg_out"],
        "combined_fee_raw": s["fee_raw"],
        "protocol_fee_pips_inferred": str(protocol_pips),
        "nominal_lp_fee_pips": str(nominal),
        "effective_lp_fee_fraction": str(lp_frac),
        "estimated_lp_fee_fire": str(fire_fee_est),
        "estimated_lp_fee_usdg": str(usdg_fee_est),
        "nearest_zero_seconds": (nz_seconds if nz_seconds is not None else ""),
        "nearest_zero_blocks": (nz_blocks if nz_blocks is not None else ""),
        "zero_same_block": zero_same_block,
        "zero_within_1min": zero_1min,
        "zero_within_5min": zero_5min,
        "zero_within_1hr": zero_1hr,
        "nearest_zero_owner": nz_owner,
        "nearest_zero_nft": nz_nft,
        "nearest_zero_tx": nz_tx,
    })

with open(OUT_SWAPS, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(correlated[0].keys()))
    writer.writeheader(); writer.writerows(correlated)

zero_rows = []
for z in liq:
    if z["action"] != "ZERO":
        continue
    prior_swaps = [s for s in swaps if s["pool"] == z["pool"] and
                   (s["_block"], s["_txi"], s["_logi"]) < (z["_block"], z["_txi"], z["_logi"])]
    if prior_swaps:
        s = max(prior_swaps, key=lambda r: (r["_block"], r["_txi"], r["_logi"]))
        seconds_after = (z["_time"] - s["_time"]).total_seconds()
        blocks_after = z["_block"] - s["_block"]
        preceding_swap_tx = s["transaction_hash"]
        preceding_direction = s["direction"]
        preceding_trader = s["tx_from"]
    else:
        seconds_after = ""; blocks_after = ""; preceding_swap_tx = ""
        preceding_direction = ""; preceding_trader = ""

    zero_rows.append({
        "pool": z["pool"], "timestamp": z["timestamp_utc"], "block": z["block"],
        "zero_tx": z["transaction_hash"], "lp_owner_tx_from": z["tx_from"],
        "nft_id": z["possible_token_id"],
        "tick_lower": z["tick_lower"], "tick_upper": z["tick_upper"],
        "preceding_swap_tx": preceding_swap_tx,
        "preceding_swap_direction": preceding_direction,
        "preceding_swap_trader": preceding_trader,
        "seconds_after_preceding_swap": seconds_after,
        "blocks_after_preceding_swap": blocks_after,
    })

with open(OUT_ZERO, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(zero_rows[0].keys()))
    writer.writeheader(); writer.writerows(zero_rows)

timeline_rows = []
for x in timeline:
    if x["kind"] == "SWAP":
        timeline_rows.append({
            "timestamp": x["timestamp_utc"], "block": x["block"], "pool": x["pool"],
            "event": "SWAP", "subtype": x["direction"], "tx": x["transaction_hash"],
            "actor": x["tx_from"], "nft": "",
            "fire_amount": (x["fire_in"] if x["direction"] == "FIRE_TO_USDG" else x["fire_out"]),
            "usdg_amount": (x["usdg_out"] if x["direction"] == "FIRE_TO_USDG" else x["usdg_in"]),
        })
    else:
        timeline_rows.append({
            "timestamp": x["timestamp_utc"], "block": x["block"], "pool": x["pool"],
            "event": "LIQUIDITY", "subtype": x["action"], "tx": x["transaction_hash"],
            "actor": x["tx_from"], "nft": x["possible_token_id"],
            "fire_amount": "", "usdg_amount": "",
        })

with open(OUT_TIMELINE, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(timeline_rows[0].keys()))
    writer.writeheader(); writer.writerows(timeline_rows)

print()
print("=" * 80); print("SWAP -> ZERO CORRELATION"); print("=" * 80)
for pool in sorted(set(r["pool"] for r in correlated)):
    rows = [r for r in correlated if r["pool"] == pool]
    n = len(rows)
    same_block = sum(str(r["zero_same_block"]) == "True" for r in rows)
    m1 = sum(str(r["zero_within_1min"]) == "True" for r in rows)
    m5 = sum(str(r["zero_within_5min"]) == "True" for r in rows)
    h1 = sum(str(r["zero_within_1hr"]) == "True" for r in rows)
    print(); print(pool)
    print(f"  swaps:                 {n}")
    print(f"  ZERO same block:       {same_block} ({same_block/n:.1%})")
    print(f"  ZERO within 1 minute:  {m1} ({m1/n:.1%})")
    print(f"  ZERO within 5 minutes: {m5} ({m5/n:.1%})")
    print(f"  ZERO within 1 hour:    {h1} ({h1/n:.1%})")

print(); print("=" * 80); print("ZERO EVENT TIMING"); print("=" * 80)
for pool in sorted(set(r["pool"] for r in zero_rows)):
    rows = [r for r in zero_rows if r["pool"] == pool]
    same_block = sum(r["blocks_after_preceding_swap"] == 0 for r in rows)
    within_60 = sum(isinstance(r["seconds_after_preceding_swap"], (int, float)) and r["seconds_after_preceding_swap"] <= 60 for r in rows)
    within_300 = sum(isinstance(r["seconds_after_preceding_swap"], (int, float)) and r["seconds_after_preceding_swap"] <= 300 for r in rows)
    within_3600 = sum(isinstance(r["seconds_after_preceding_swap"], (int, float)) and r["seconds_after_preceding_swap"] <= 3600 for r in rows)
    print(); print(pool)
    print(f"  ZERO events:           {len(rows)}")
    print(f"  same block as swap:    {same_block}")
    print(f"  <= 1 minute:           {within_60}")
    print(f"  <= 5 minutes:          {within_300}")
    print(f"  <= 1 hour:             {within_3600}")

print(); print("=" * 80); print("LP OPERATORS"); print("=" * 80)
owner_stats = defaultdict(lambda: {"events": 0, "adds": 0, "removes": 0, "zeros": 0, "nfts": set(), "pools": set()})
for r in liq:
    a = r["tx_from"].lower(); st = owner_stats[a]
    st["events"] += 1; st["pools"].add(r["pool"]); st["nfts"].add(r["possible_token_id"])
    if r["action"] == "ADD": st["adds"] += 1
    elif r["action"] == "REMOVE": st["removes"] += 1
    elif r["action"] == "ZERO": st["zeros"] += 1

for addr, st in sorted(owner_stats.items(), key=lambda kv: kv[1]["events"], reverse=True):
    print(); print(addr)
    print(f"  events:   {st['events']}")
    print(f"  ADD:      {st['adds']}")
    print(f"  REMOVE:   {st['removes']}")
    print(f"  ZERO:     {st['zeros']}")
    print(f"  NFTs:     {len(st['nfts'])}")
    print(f"  pools:    {', '.join(sorted(st['pools']))}")

print(); print("=" * 80); print("ROUTER / SWAP SENDER DISTRIBUTION"); print("=" * 80)
router_counts = Counter(r["swap_sender"].lower() for r in swaps)
for addr, count in router_counts.most_common():
    print(f"{count:4}  {addr}")

traders = set(r["tx_from"].lower() for r in swaps)
lp_operators = set(r["tx_from"].lower() for r in liq)
overlap = traders & lp_operators

print(); print("=" * 80); print("TRADER / LP OVERLAP"); print("=" * 80)
print(f"unique traders:      {len(traders)}")
print(f"unique LP operators: {len(lp_operators)}")
print(f"overlap:             {len(overlap)}")
for a in sorted(overlap):
    swap_count = sum(r["tx_from"].lower() == a for r in swaps)
    lp_count = sum(r["tx_from"].lower() == a for r in liq)
    print(f"{a}  swaps={swap_count} LP_events={lp_count}")

print(); print("=" * 80); print("ESTIMATED LP-SIDE FEES"); print("=" * 80)
for pool in POOLS if False else sorted(set(r["pool"] for r in correlated)):
    rows = [r for r in correlated if r["pool"] == pool]
    fire_fee = sum(Decimal(r["estimated_lp_fee_fire"]) for r in rows)
    usdg_fee = sum(Decimal(r["estimated_lp_fee_usdg"]) for r in rows)
    print(); print(pool)
    print(f"  estimated FIRE fees: {fire_fee:,.6f}")
    print(f"  estimated USDG fees: {usdg_fee:,.6f}")

print(); print("Wrote:")
print(f"  {OUT_SWAPS}")
print(f"  {OUT_ZERO}")
print(f"  {OUT_TIMELINE}")
