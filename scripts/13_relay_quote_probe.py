"""Black-box Relay quote probe for FIRE on Robinhood Chain (chain 4663).

Requests executable quotes across a size sweep and under swap-source controls.
Nothing is broadcast. The /quote and /swap-sources endpoints answer without an
API key (unlike /requests/v3, which requires one).

For each quote we record what Relay says it expects to receive on the ORIGIN
side -- protocol.v2.orderData.inputs[].payment.amount, denominated in USDG.
That is the field directly comparable to the historical RelayErc20Deposit
amounts, i.e. the quote-vs-delivered comparison the on-chain study could not see.
"""
import csv, json, subprocess, time
from decimal import Decimal, getcontext

getcontext().prec = 40

API = "https://api.relay.link"
CHAIN = 4663
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
DEST_CHAIN = 8453
DEST_CURRENCY = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"   # USDC on Base
USER = "0xb1cc9e0cd3d51a582b7bd66049aee8854fdba408"            # a real observed FIRE seller

SIZES = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 20_000,
         40_000, 60_000, 80_000, 100_000]

OUT = "data/analysis/RELAY_QUOTE_PROBE.csv"


def post(path, body, tries=3):
    payload = json.dumps(body)
    last = None
    for a in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "60", "-X", "POST",
                                f"{API}{path}", "-H", "Content-Type: application/json",
                                "-d", payload], capture_output=True, text=True, check=True)
            return json.loads(p.stdout)
        except Exception as e:
            last = e; time.sleep(1.0 * (a + 1))
    raise last


def get(path, tries=3):
    last = None
    for a in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "40", f"{API}{path}"],
                               capture_output=True, text=True, check=True)
            return json.loads(p.stdout)
        except Exception as e:
            last = e; time.sleep(1.0 * (a + 1))
    raise last


sources = get(f"/swap-sources?chainId={CHAIN}").get("sources", [])
print(f"Relay origin swap sources on chain {CHAIN}: {sources}\n")

# default, plus one variant per available source (include-only and exclude)
VARIANTS = [("default", {})]
for s in sources:
    VARIANTS.append((f"only:{s}", {"includedOriginSwapSources": [s]}))
    VARIANTS.append((f"not:{s}", {"excludedOriginSwapSources": [s]}))

rows = []
for size in SIZES:
    amount = str(int(Decimal(size) * Decimal(10 ** 18)))
    for label, extra in VARIANTS:
        body = {"user": USER, "recipient": USER,
                "originChainId": CHAIN, "originCurrency": FIRE,
                "destinationChainId": DEST_CHAIN, "destinationCurrency": DEST_CURRENCY,
                "amount": amount, "tradeType": "EXACT_INPUT", **extra}
        try:
            q = post("/quote", body)
        except Exception as e:
            print(f"  {size:>7} {label:<18} REQUEST FAILED {str(e)[:60]}")
            continue
        if "steps" not in q:
            msg = q.get("message") or q.get("error") or json.dumps(q)[:90]
            rows.append({"fire_in": size, "variant": label, "ok": False,
                         "error": str(msg)[:200], "origin_usdg_expected": "",
                         "dest_out": "", "dest_symbol": "", "swap_impact_pct": "",
                         "total_impact_pct": "", "rate": "", "order_id": "",
                         "solver": "", "relayer_fee_usdg": "", "deposit_to": "",
                         "calldata_len": ""})
            print(f"  {size:>7} {label:<18} no-route: {str(msg)[:70]}")
            continue
        det = q.get("details", {})
        proto = (q.get("protocol") or {}).get("v2", {}) or {}
        od = proto.get("orderData") or {}
        ins = od.get("inputs") or []
        origin_usdg = sum(Decimal(i["payment"]["amount"]) for i in ins
                          if i.get("payment", {}).get("currency", "").lower() == USDG)
        origin_usdg = origin_usdg / Decimal(10 ** 6) if ins else Decimal(0)
        dep = next((s for s in q["steps"] if s["id"] == "deposit"), None)
        data = (dep["items"][0]["data"] if dep and dep.get("items") else {}) or {}
        fees = q.get("fees") or {}
        rows.append({
            "fire_in": size, "variant": label, "ok": True, "error": "",
            "origin_usdg_expected": str(origin_usdg),
            "dest_out": (det.get("currencyOut") or {}).get("amountFormatted", ""),
            "dest_symbol": ((det.get("currencyOut") or {}).get("currency") or {}).get("symbol", ""),
            "swap_impact_pct": (det.get("swapImpact") or {}).get("percent", ""),
            "total_impact_pct": (det.get("totalImpact") or {}).get("percent", ""),
            "rate": det.get("rate", ""),
            "order_id": proto.get("orderId", ""),
            "solver": od.get("solver", ""),
            "relayer_fee_usdg": (fees.get("relayer") or {}).get("amountFormatted", ""),
            "deposit_to": data.get("to", ""),
            "calldata_len": len(data.get("data", "") or ""),
        })
        r = rows[-1]
        print(f"  {size:>7} {label:<18} origin={origin_usdg:>10.6f} USDG  "
              f"dest={r['dest_out']:>12} {r['dest_symbol']:<5} "
              f"impact={r['swap_impact_pct']:>7}%")
        time.sleep(0.25)

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"\nWrote {OUT} ({len(rows)} rows)")

ok = [r for r in rows if r["ok"]]
print(f"\nquotes returned: {len(ok)}/{len(rows)}")
print("\nDEFAULT variant, origin-side USDG per FIRE by size:")
for r in [x for x in ok if x["variant"] == "default"]:
    px = Decimal(r["origin_usdg_expected"]) / Decimal(r["fire_in"])
    print(f"  {r['fire_in']:>7} FIRE -> {Decimal(r['origin_usdg_expected']):>10.6f} USDG"
          f"   {px:.10f} USDG/FIRE   impact {r['swap_impact_pct']}%")
