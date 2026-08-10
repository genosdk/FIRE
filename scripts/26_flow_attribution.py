"""Post-processing for the main lifecycle sample: three-way flow attribution.

ROUTER_LIKE_FLOW conflates identified routing infrastructure with unlabelled
high-prevalence senders that may be arbitrage or market-making bots. This splits
them so the fee gradient can be tested specifically against identifiable routing
infrastructure:

  KNOWN_INFRA_FLOW        sender is an identified router / settler contract
  UNKNOWN_PREVALENT_FLOW  meets the prevalence threshold, not known infra
  OTHER_FLOW              everything else

Throughout, POOL SUCCESS PROBABILITY (share of pools receiving any flow) is
reported separately from ECONOMIC FLOW CAPTURE (share of routed swaps captured).
A configuration can win rarely but win large. "Volume" is routed swap COUNT, not
token amounts, because competing pools use different base assets.

No RPC work -- reads the cached raw logs.
"""
import csv, json, os, statistics, sys
from collections import Counter, defaultdict

CACHE = "data/analysis/_lifecycle_val_cache.json"
POOLS = "data/analysis/LIFECYCLE_MAIN_POOLS.csv"
OUT = "data/analysis/LIFECYCLE_FLOW_ATTRIBUTION.csv"
THRESHOLDS = (3, 5, 10)

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
BANDS = ((100000, 400000, "10-40%"), (400000, 700000, "40-70%"),
         (700000, 900000, "70-90%"), (900000, 10 ** 9, "90%+"))

if not os.path.exists(POOLS):
    sys.exit(f"{POOLS} not present -- main sample retrieval has not finished")
cache = json.load(open(CACHE))
rows = list(csv.DictReader(open(POOLS)))
print(f"pools: {len(rows):,}   cached raw records: {len(cache):,}")

sender_pools = defaultdict(set)
for r in rows:
    for s in cache[r["pool_id"]]["swaps"]:
        sender_pools[s["s"]].add(r["pool_id"])
RL = {th: {s for s, ps in sender_pools.items() if len(ps) >= th} for th in THRESHOLDS}
UNK = {th: RL[th] - set(KNOWN_INFRA) for th in THRESHOLDS}
print(f"distinct senders: {len(sender_pools):,}")
for th in THRESHOLDS:
    print(f"  t={th:<2}  prevalent {len(RL[th]):>4}  known {len(RL[th] & set(KNOWN_INFRA)):>2}  "
          f"unknown-prevalent {len(UNK[th]):>4}")

out = []
for r in rows:
    sw = sorted(cache[r["pool_id"]]["swaps"], key=lambda x: x["b"])
    mods = sorted(cache[r["pool_id"]]["mods"], key=lambda x: x["b"])
    seed = next((m for m in mods if m["dl"] > 0), None)
    salt = seed["salt"] if seed else None
    e = dict(r)
    e["fee"] = int(r["fee"]); e["deployment_rank"] = int(r["deployment_rank"])
    e["n_swaps"] = len(sw)
    k = [s for s in sw if s["s"] in KNOWN_INFRA]
    e["n_known"] = len(k)
    e["known_flow"] = len(k) > 0
    for th in THRESHOLDS:
        u = [s for s in sw if s["s"] in UNK[th]]
        e[f"n_unknown_prev_t{th}"] = len(u)
        e[f"unknown_prev_flow_t{th}"] = len(u) > 0
        e[f"n_router_like_t{th}"] = len(k) + len(u)
    e["n_other"] = len(sw) - len(k) - e["n_unknown_prev_t5"]
    fk = k[0]["b"] if k else None
    e["own_harvest_after_known"] = sum(1 for m in mods if m["dl"] == 0 and fk and m["b"] > fk
                                       and salt is not None and m["salt"] == salt)
    e["own_scale_after_known"] = sum(1 for m in mods if m["dl"] > 0 and fk and m["b"] > fk
                                     and salt is not None and m["salt"] == salt)
    e["foreign_poke_after_known"] = sum(1 for m in mods if m["dl"] == 0 and fk and m["b"] > fk
                                        and (salt is None or m["salt"] != salt))
    out.append(e)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
print(f"Wrote {OUT}\n")

def band_of(fee):
    for lo, hi, lab in BANDS:
        if lo <= fee < hi: return lab
    return "?"

TK = sum(r["n_known"] for r in out) or 1
TU = sum(r["n_unknown_prev_t5"] for r in out) or 1

print("=" * 84)
print("1-2. FEE GRADIENT: pool success probability")
print("=" * 84)
print(f"  {'band':10} {'pools':>7} {'known infra':>13} {'unknown prev':>14} {'combined':>11}")
for lo, hi, lab in BANDS:
    sub = [r for r in out if lo <= r["fee"] < hi]
    if not sub: continue
    kk = sum(1 for r in sub if r["known_flow"])
    uu = sum(1 for r in sub if r["unknown_prev_flow_t5"])
    cc = sum(1 for r in sub if r["known_flow"] or r["unknown_prev_flow_t5"])
    print(f"  {lab:10} {len(sub):>7,} {kk/len(sub)*100:>12.1f}% {uu/len(sub)*100:>13.1f}% "
          f"{cc/len(sub)*100:>10.1f}%")

print("\n" + "=" * 84)
print("FEE GRADIENT: economic flow capture (share of routed swaps)")
print("=" * 84)
print(f"  {'band':10} {'known-infra vol':>17} {'unknown-prev vol':>18}")
for lo, hi, lab in BANDS:
    sub = [r for r in out if lo <= r["fee"] < hi]
    if not sub: continue
    print(f"  {lab:10} {sum(r['n_known'] for r in sub)/TK*100:>16.1f}% "
          f"{sum(r['n_unknown_prev_t5'] for r in sub)/TU*100:>17.1f}%")

print("\n" + "=" * 84)
print("3. HOW MUCH ROUTER-LIKE VOLUME IS ACTUALLY KNOWN INFRASTRUCTURE?")
print("=" * 84)
for th in THRESHOLDS:
    tot = sum(r["n_known"] + r[f"n_unknown_prev_t{th}"] for r in out) or 1
    print(f"  t={th:<2}  known-infra share of router-like volume: "
          f"{sum(r['n_known'] for r in out)/tot*100:.1f}%")
print(f"\n  {'band':10} {'known share of router-like volume':>36}")
for lo, hi, lab in BANDS:
    sub = [r for r in out if lo <= r["fee"] < hi]
    tot = sum(r["n_known"] + r["n_unknown_prev_t5"] for r in sub) or 1
    if sub:
        print(f"  {lab:10} {sum(r['n_known'] for r in sub)/tot*100:>35.1f}%")

print("\n" + "=" * 84)
print("4. DEPLOYMENT RANK AT SCALE")
print("=" * 84)
def bk(k): return "1" if k == 1 else "2" if k == 2 else "3" if k == 3 else \
    "4-5" if k <= 5 else "6-9" if k <= 9 else "10+"
g = defaultdict(list)
for r in out: g[bk(r["deployment_rank"])].append(r)
print(f"  {'rank':6} {'pools':>7} {'known infra':>13} {'unknown prev':>14} "
      f"{'% known vol':>13} {'% unk vol':>11}")
for b in ("1", "2", "3", "4-5", "6-9", "10+"):
    sub = g.get(b)
    if not sub: continue
    kk = sum(1 for r in sub if r["known_flow"]); uu = sum(1 for r in sub if r["unknown_prev_flow_t5"])
    print(f"  {b:6} {len(sub):>7,} {kk/len(sub)*100:>12.1f}% {uu/len(sub)*100:>13.1f}% "
          f"{sum(r['n_known'] for r in sub)/TK*100:>12.1f}% "
          f"{sum(r['n_unknown_prev_t5'] for r in sub)/TU*100:>10.1f}%")

print("\n" + "=" * 84)
print("5. LOWEST-FEE POOL WITHIN EACH OPPORTUNITY")
print("=" * 84)
byt = defaultdict(list)
for r in out: byt[r["token"]].append(r)
for lab, key, nk in (("known infra", "n_known", "known_flow"),
                     ("unknown prevalent", "n_unknown_prev_t5", "unknown_prev_flow_t5")):
    wins, shares, deltas, franks = 0, [], [], []
    n = 0
    for t, rs in byt.items():
        if len(rs) < 2: continue
        tot = sum(r[key] for r in rs)
        if tot == 0: continue
        n += 1
        mn = min(r["fee"] for r in rs)
        lows = [r for r in rs if r["fee"] == mn]
        if any(r[nk] for r in lows): wins += 1
        shares.append(sum(r[key] for r in lows) / tot)
        win = max(rs, key=lambda r: r[key])
        deltas.append((win["fee"] - mn) / 10000)
        franks.append(sorted(set(r["fee"] for r in rs)).index(win["fee"]) + 1)
    if not n: continue
    shares.sort()
    print(f"\n  -- {lab} --  opportunities with flow: {n:,}")
    print(f"     lowest-fee pool received flow: {wins:,}/{n:,} ({wins/n*100:.1f}%)")
    print(f"     lowest-fee pool volume share:  median {shares[len(shares)//2]*100:.1f}%, "
          f"mean {sum(shares)/len(shares)*100:.1f}%")
    print(f"     winner fee - min fee: median {statistics.median(deltas):+.2f} pp")
    print(f"     winner fee rank (1=cheapest): {Counter(franks).most_common(5)}")

print("\n" + "=" * 84)
print("6. OPERATOR BEHAVIOUR AFTER KNOWN-INFRA FLOW")
print("=" * 84)
withflow = [r for r in out if r["known_flow"]]
print(f"  pools receiving known-infra flow: {len(withflow):,}")
if withflow:
    h = sum(1 for r in withflow if r["own_harvest_after_known"] > 0)
    s = sum(1 for r in withflow if r["own_scale_after_known"] > 0)
    fp = sum(1 for r in withflow if r["foreign_poke_after_known"] > 0)
    print(f"    seed operator harvested (own salt): {h:,} ({h/len(withflow)*100:.1f}%)")
    print(f"    seed operator scaled  (own salt):   {s:,} ({s/len(withflow)*100:.1f}%)")
    print(f"    poked by a DIFFERENT position:      {fp:,} ({fp/len(withflow)*100:.1f}%)")
    noflow = [r for r in out if not r["known_flow"]]
    hn = sum(1 for r in noflow if r["own_harvest_after_known"] > 0)
    print(f"  pools WITHOUT known-infra flow: {len(noflow):,}; harvested {hn:,} "
          f"(by construction 0 -- harvest is measured only after flow)")
