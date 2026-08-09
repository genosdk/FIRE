"""Operator wallet-tree tracing for the extreme-fee FIRE/USDG LPs.

For each LP operator address: transaction counters, the earliest inbound native
transfer (the funder), and the full counterparty set. Shared funders or shared
counterparties across operators are the evidence for a single coordinated
cluster versus independent strategies.

Uses Blockscout (native transfers are not logs, so eth_getLogs cannot see them,
and the RPC retains only ~6,200 blocks of state).
"""
import csv, json, subprocess, time
from collections import defaultdict, Counter

BS = "https://robinhoodchain.blockscout.com"
LIQ = "data/liquidity/FIRE_USDG_ALL_modify_liquidity.csv"
OUT = "data/analysis/LP_OPERATOR_CLUSTER.csv"
OUT_LINKS = "data/analysis/LP_OPERATOR_LINKS.csv"
MAX_PAGES = 40

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
}


def get(path, tries=3):
    for a in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "35", f"{BS}{path}"],
                               capture_output=True, text=True, check=True)
            return json.loads(p.stdout)
        except Exception:
            time.sleep(0.5 * (a + 1))
    return None


ops = sorted({r["tx_from"].lower() for r in csv.DictReader(open(LIQ))})
print(f"LP operators: {len(ops)}\n")

rows = []
counterparties = {}
for i, a in enumerate(ops, 1):
    c = get(f"/api/v2/addresses/{a}/counters") or {}
    ntx = c.get("transactions_count", "?")
    info = get(f"/api/v2/addresses/{a}") or {}
    # page to the oldest inbound transaction
    oldest = None
    cps = Counter()
    params = ""
    pages = 0
    while pages < MAX_PAGES:
        d = get(f"/api/v2/addresses/{a}/transactions{params}")
        if not d or "items" not in d:
            break
        for t in d["items"]:
            f = ((t.get("from") or {}).get("hash") or "").lower()
            to = ((t.get("to") or {}).get("hash") or "").lower()
            other = to if f == a else f
            if other:
                cps[other] += 1
            if f != a:
                oldest = t          # list is newest-first, so last seen inbound is oldest so far
        np = d.get("next_page_params")
        pages += 1
        if not np:
            break
        params = "?" + "&".join(f"{k}={v}" for k, v in np.items() if v is not None)
    funder = ((oldest or {}).get("from") or {}).get("hash", "")
    funder = funder.lower() if funder else ""
    counterparties[a] = cps
    rows.append({"operator": a, "tx_count": ntx,
                 "is_contract": info.get("is_contract"),
                 "name": info.get("name") or "",
                 "first_funder": funder,
                 "first_funder_label": KNOWN.get(funder, ""),
                 "first_seen_tx": (oldest or {}).get("hash", ""),
                 "first_seen_at": (oldest or {}).get("timestamp", ""),
                 "distinct_counterparties": len(cps),
                 "pages_scanned": pages})
    print(f"[{i:02}/{len(ops)}] {a}  txs={ntx}  funder={funder or '?'} "
          f"{KNOWN.get(funder,'')}  counterparties={len(cps)}")

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

# shared non-infrastructure counterparties between operators
opset = set(ops)
links = []
for i, a in enumerate(ops):
    for b in ops[i + 1:]:
        shared = set(counterparties.get(a, {})) & set(counterparties.get(b, {}))
        shared = {s for s in shared if s not in KNOWN and s not in opset}
        if shared:
            links.append({"operator_a": a, "operator_b": b, "shared_count": len(shared),
                          "shared": "|".join(sorted(shared)[:12])})
links.sort(key=lambda r: -r["shared_count"])
if links:
    with open(OUT_LINKS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(links[0].keys())); w.writeheader(); w.writerows(links)

print(f"\nWrote {OUT}" + (f" and {OUT_LINKS}" if links else " (no shared-counterparty links found)"))
print("\nFunder frequency:")
for k, v in Counter(r["first_funder"] for r in rows if r["first_funder"]).most_common():
    print(f"  {v:2}  {k}  {KNOWN.get(k,'')}")
print("\nStrongest operator links (shared non-infrastructure counterparties):")
for l in links[:10]:
    print(f"  {l['shared_count']:3}  {l['operator_a'][:12]}… <-> {l['operator_b'][:12]}…")
