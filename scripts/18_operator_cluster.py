"""Operator wallet-tree tracing for the extreme-fee FIRE/USDG LPs.

Identifies, for each LP operator: the funding transaction that first credited the
address (its funder), and its counterparty set. Shared funders or shared
non-infrastructure counterparties across operators are the evidence for a single
coordinated cluster rather than independent strategies.

Native transfers are not logs and the RPC retains only ~6,200 blocks of state,
so Blockscout is the only source. Uses the v1 (etherscan-compatible) API because
it supports sort=asc, which returns the oldest transaction directly -- the v2 API
pages newest-first and some operators have thousands of transactions.
Blockscout rate-limits aggressively, hence the pacing and backoff.
"""
import csv, json, subprocess, time
from collections import Counter, defaultdict

BS = "https://robinhoodchain.blockscout.com"
LIQ = "data/liquidity/FIRE_USDG_ALL_modify_liquidity.csv"
OUT = "data/analysis/LP_OPERATOR_CLUSTER.csv"
OUT_LINKS = "data/analysis/LP_OPERATOR_LINKS.csv"
PACE = 6.0

KNOWN = {
    "0x8366a39cc670b4001a1121b8f6a443a643e40951": "UniV4 PoolManager",
    "0x58daec3116aae6d93017baaea7749052e8a04fa7": "UniV4 PositionManager",
    "0x8876789976decbfcbbbe364623c63652db8c0904": "Uniswap UniversalRouter",
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Permit2",
    "0x4cd00e387622c35bddb9b4c962c136462338bc31": "Relay Depository",
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be": "RelayApprovalProxyV3",
    "0xf70da97812cb96acdf810712aa562db8dfa3dbef": "Relay solver",
    "0x1d4b86491ec211257cbedd77a4380a7494624eff": "RobinHoodSettler (0x)",
    "0xaa61254627b7392b0bc922097b10eb0587db2be7": "RobinHoodSettler (0x)",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f": "RobinHoodSettler (0x)",
    "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
    "0x43eea882b845a8493152ebc55cf30ae9281b02d5": "FIRE token",
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG token",
}


def api(params, tries=8):
    """v1 API call with backoff on rate limiting."""
    url = f"{BS}/api?" + "&".join(f"{k}={v}" for k, v in params.items())
    for a in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "35", url],
                               capture_output=True, text=True, check=True)
            d = json.loads(p.stdout)
            if isinstance(d, dict) and "Too many requests" in str(d.get("message", "")):
                time.sleep(6.0 * (a + 1)); continue
            return d
        except Exception:
            time.sleep(2.0 * (a + 1))
    return None


ops = sorted({r["tx_from"].lower() for r in csv.DictReader(open(LIQ))})
print(f"LP operators: {len(ops)}\n")

rows = []
cps = {}
for i, a in enumerate(ops, 1):
    first_ext = api({"module": "account", "action": "txlist", "address": a,
                     "page": 1, "offset": 5, "sort": "asc"})
    time.sleep(PACE)
    first_int = api({"module": "account", "action": "txlistinternal", "address": a,
                     "page": 1, "offset": 5, "sort": "asc"})
    time.sleep(PACE)

    fail = []
    if not first_ext or first_ext.get("status") != "1": fail.append("txlist")
    if not first_int or first_int.get("status") != "1": fail.append("internal")

    def first_inbound(res):
        if not res or res.get("status") != "1" or not isinstance(res.get("result"), list):
            return None
        for t in res["result"]:
            if (t.get("to") or "").lower() == a and (t.get("from") or "").lower() != a:
                if int(t.get("value", "0") or 0) > 0:
                    return t
        return None

    fe, fi = first_inbound(first_ext), first_inbound(first_int)
    cand = [x for x in (fe, fi) if x]
    first = min(cand, key=lambda t: int(t["timeStamp"])) if cand else None
    funder = (first or {}).get("from", "").lower()

    # counterparty census from the first N pages of external txs
    counter = Counter()
    for page in (1, 2):
        r = api({"module": "account", "action": "txlist", "address": a,
                 "page": page, "offset": 50, "sort": "asc"})
        time.sleep(PACE)
        if not r or r.get("status") != "1" or not isinstance(r.get("result"), list):
            fail.append(f"cp-page{page}")
            break
        for t in r["result"]:
            f = (t.get("from") or "").lower(); to = (t.get("to") or "").lower()
            other = to if f == a else f
            if other and other != a:
                counter[other] += 1
        if len(r["result"]) < 50:
            break
    cps[a] = counter

    rows.append({"operator": a,
                 "first_funder": funder,
                 "first_funder_label": KNOWN.get(funder, ""),
                 "first_funding_tx": (first or {}).get("hash", ""),
                 "first_funding_value_eth": str(int((first or {}).get("value", "0") or 0) / 1e18),
                 "first_funding_ts": (first or {}).get("timeStamp", ""),
                 "failed_calls": "|".join(fail),
                 "distinct_counterparties": len(counter),
                 "top_counterparties": "|".join(f"{k}:{v}" for k, v in counter.most_common(6))})
    print(f"[{i:02}/{len(ops)}] {a}  funder={funder or '?'} "
          f"{KNOWN.get(funder,'')}  cps={len(counter)}"
          + (f"  FAILED:{','.join(fail)}" if fail else ""))

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

opset = set(ops)
links = []
for i, a in enumerate(ops):
    for b in ops[i + 1:]:
        shared = set(cps.get(a, {})) & set(cps.get(b, {}))
        shared = {s for s in shared if s not in KNOWN and s not in opset}
        if shared:
            links.append({"operator_a": a, "operator_b": b, "shared_count": len(shared),
                          "shared": "|".join(sorted(shared)[:12])})
links.sort(key=lambda r: -r["shared_count"])
if links:
    with open(OUT_LINKS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(links[0].keys())); w.writeheader(); w.writerows(links)

print(f"\nWrote {OUT}" + (f" and {OUT_LINKS}" if links else "  (no shared non-infra counterparties)"))
bad = [r for r in rows if r["failed_calls"]]
print(f"\nrows with failed API calls: {len(bad)}/{len(rows)}"
      + ("  -- treat their links as incomplete" if bad else ""))
print("\nFunder frequency:")
for k, v in Counter(r["first_funder"] for r in rows if r["first_funder"]).most_common():
    tag = KNOWN.get(k, "")
    shared = " <-- SHARED FUNDER" if v > 1 else ""
    print(f"  {v:2}  {k}  {tag}{shared}")
print("\nOperator pairs by shared non-infrastructure counterparties:")
for l in links[:12]:
    print(f"  {l['shared_count']:3}  {l['operator_a'][:14]}… <-> {l['operator_b'][:14]}…")
