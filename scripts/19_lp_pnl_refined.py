"""Refined LP P&L: contemporaneous valuation, liquidation-aware residual,
and maximum concurrent capital at risk.

Three corrections to 17_lp_pnl.py:

1. CONTEMPORANEOUS VALUATION. Every FIRE cash flow is valued at the canonical
   FIRE/USDG price at its own block, not at today's price. The canonical price
   series is built from the canonical ETH/FIRE pool's Swap events (giving
   ETH per FIRE) multiplied by an ETH/USDG series from a low-fee ETH/USDG v4
   pool. Each LP transaction takes the nearest PRIOR observation from both.

2. LIQUIDATION-AWARE RESIDUAL. Remaining inventory is valued by quoting the
   actual sale of the full FIRE balance into canonical liquidity via the v4
   Quoter, instead of marking it at spot with zero exit slippage.

3. MAXIMUM CONCURRENT CAPITAL. Tracks the running net capital at risk over
   time and reports its peak, so recycled fees are not counted as fresh
   external capital. Cumulative gross contributions overstate what the
   operator actually had to fund.
"""
import csv, json, os, subprocess, time
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal, getcontext
from eth_abi import encode, decode
from eth_utils import keccak

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
NATIVE = "0x0000000000000000000000000000000000000000"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
CANON = "0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
POOLS = {"FIRE_USDG_50pct": "0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43",
         "FIRE_USDG_40pct": "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636"}
LIQ = "data/liquidity/FIRE_USDG_ALL_modify_liquidity.csv"
DELTAS = "data/analysis/_lp_tx_deltas.json"
PRICES = "data/analysis/_canonical_price_series.json"
OUT_POS = "data/analysis/LP_PNL_REFINED_BY_POSITION.csv"
OUT_OP = "data/analysis/LP_PNL_REFINED_BY_OPERATOR.csv"
START = 13_400_000

SEL = keccak(text="quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")[:4]
ARG = "((address,address,uint24,int24,address),bool,uint128,bytes)"


def rpc(m, p, tries=4):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "45", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.3 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.3 * (a + 1))
    raise last


def i24(w):
    v = int(w, 16) & 0xFFFFFF
    return v - 0x1000000 if v & 0x800000 else v


def quote(key, zfo, amt):
    data = "0x" + (SEL + encode([ARG], [(key, zfo, int(amt), b"")])).hex()
    try:
        res = rpc("eth_call", [{"to": QUOTER, "data": data, "gas": hex(30_000_000)}, "latest"], tries=2)
        return decode(["uint256", "uint256"], bytes.fromhex(res[2:]))[0]
    except Exception:
        return None


def chunked(addr, topics, lo, hi, chunk):
    out, s = [], lo
    while s <= hi:
        e = min(s + chunk - 1, hi)
        for a in range(4):
            try:
                out += rpc("eth_getLogs", [{"address": addr, "fromBlock": hex(s),
                                            "toBlock": hex(e), "topics": topics}], tries=1)
                break
            except Exception:
                time.sleep(0.4 * (a + 1))
        s = e + 1
    return out


latest = int(rpc("eth_blockNumber", []), 16)

# ---- pool keys ----------------------------------------------------------
lg = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest", "topics": [INIT, CANON]}])[0]
w = [lg["data"][2:][i:i + 64] for i in range(0, len(lg["data"][2:]), 64)]
CK = (NATIVE, FIRE, int(w[0], 16) & 0xFFFFFF, i24(w[1]), "0x" + w[2][24:])

nat_t = "0x" + "00" * 32
usdg_t = "0x" + "00" * 12 + USDG[2:]
ethusdg = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                               "topics": [INIT, None, nat_t, usdg_t]}])
best, bkey, bpid = 0, None, None
for L in ethusdg:
    d = L["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
    fee = int(ws[0], 16) & 0xFFFFFF
    if fee > 3000: continue
    k = (NATIVE, USDG, fee, i24(ws[1]), "0x" + ws[2][24:])
    o = quote(k, True, 10 ** 16)
    if o and o > best:
        best, bkey, bpid = o, k, L["topics"][1].lower()
print(f"canonical ETH/FIRE fee={CK[2]}   bridge ETH/USDG fee={bkey[2]} pool={bpid[:14]}…")

# ---- price series -------------------------------------------------------
if os.path.exists(PRICES):
    ser = json.load(open(PRICES))
    print(f"loaded cached price series ({len(ser['fire'])} FIRE, {len(ser['eth'])} ETH obs)")
else:
    print("building canonical price series...")
    cl = chunked(PM, [SWAP, CANON], START, latest, 250_000)
    fire = []                                   # (block, ETH per FIRE)
    for L in cl:
        d = L["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
        sp = Decimal(int(ws[2], 16)) / Decimal(2 ** 96)
        if sp <= 0: continue
        fire.append((int(L["blockNumber"], 16), str(1 / (sp * sp))))   # currency1=FIRE per ETH -> invert
    el = chunked(PM, [SWAP, bpid], START, latest, 250_000)
    eth = []                                    # (block, USDG per ETH)
    for L in el:
        d = L["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
        sp = Decimal(int(ws[2], 16)) / Decimal(2 ** 96)
        if sp <= 0: continue
        eth.append((int(L["blockNumber"], 16), str(sp * sp * Decimal(10 ** 12))))
    ser = {"fire": fire, "eth": eth}
    json.dump(ser, open(PRICES, "w"))
    print(f"  {len(fire)} canonical obs, {len(eth)} ETH/USDG obs")

fb = [x[0] for x in ser["fire"]]; fv = [Decimal(x[1]) for x in ser["fire"]]
eb = [x[0] for x in ser["eth"]]; ev = [Decimal(x[1]) for x in ser["eth"]]


def price_at(block):
    """canonical USDG per FIRE at `block`, using the nearest prior observation."""
    i = bisect_right(fb, block) - 1
    j = bisect_right(eb, block) - 1
    if i < 0 or j < 0:
        return None
    return fv[i] * ev[j]


# spot price now, for reference
P_NOW = price_at(latest)
print(f"canonical FIRE price now: {P_NOW:.12f} USDG/FIRE")

# ---- deltas -------------------------------------------------------------
delta = {k: {"fire": Decimal(v["fire"]), "usdg": Decimal(v["usdg"])}
         for k, v in json.load(open(DELTAS)).items()}
liq = list(csv.DictReader(open(LIQ)))
blk_of = {r["transaction_hash"]: int(r["block"]) for r in liq}
tx_action = defaultdict(set)
for r in liq:
    tx_action[r["transaction_hash"]].add(r["action"])

# ---- residual liquidity per position ------------------------------------
def s128(x):
    v = int(x, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >= 1 << 127 else v

sqrtP = {}
for name, pid in POOLS.items():
    lgs = chunked(PM, [SWAP, pid], START, latest, 400_000)
    d = lgs[-1]["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
    sqrtP[name] = Decimal(int(ws[2], 16)) / Decimal(2 ** 96)
SA = Decimal(1.0001) ** (Decimal(-880000) / 2)
SB = Decimal(1.0001) ** (Decimal(880000) / 2)

pos = defaultdict(lambda: {"pool": "", "owner": "", "liq": 0, "flows": [],
                           "dep": Decimal(0), "wd": Decimal(0), "fee": Decimal(0),
                           "first": "", "last": ""})
seen = set()
for r in liq:
    key = (r["pool"], r["possible_token_id"])
    p = pos[key]
    p["pool"] = r["pool"]; p["owner"] = r["tx_from"].lower()
    p["liq"] += int(r["liquidity_delta"])
    if not p["first"]: p["first"] = r["timestamp_utc"]
    p["last"] = r["timestamp_utc"]
    tk = (r["transaction_hash"], key)
    if tk in seen: continue
    seen.add(tk)
    tx = r["transaction_hash"]; blk = blk_of[tx]
    px = price_at(blk) or P_NOW
    d = delta[tx]
    v = d["fire"] * px + d["usdg"]              # contemporaneous value, signed
    p["flows"].append((blk, v))
    if v < 0:
        p["dep"] += -v
    elif tx_action[tx] == {"ZERO"}:
        p["fee"] += v
    else:
        p["wd"] += v

# ---- liquidation-aware residual -----------------------------------------
resid_fire = {}
for (pool, tid), p in pos.items():
    L = Decimal(p["liq"]); S = sqrtP[pool]
    rf = L * (Decimal(1) / S - Decimal(1) / SB) / Decimal(10 ** 18) if L > 0 else Decimal(0)
    ru = L * (S - SA) / Decimal(10 ** 6) if L > 0 else Decimal(0)
    resid_fire[(pool, tid)] = (rf, ru)

print("\nliquidation quotes for aggregate residual FIRE:")
liq_rate = {}
for pool in POOLS:
    total = sum(resid_fire[k][0] for k in resid_fire if k[0] == pool)
    if total <= 0:
        liq_rate[pool] = P_NOW; continue
    eth_out = quote(CK, False, int(total * Decimal(10 ** 18)))
    usdg_out = quote(bkey, True, eth_out) if eth_out else None
    if usdg_out:
        eff = Decimal(usdg_out) / Decimal(10 ** 6) / total
        liq_rate[pool] = eff
        print(f"  {pool}: {total:,.0f} FIRE -> {Decimal(usdg_out)/Decimal(10**6):,.4f} USDG "
              f"= {eff:.12f}/FIRE  ({(eff/P_NOW-1)*100:+.2f}% vs spot)")
    else:
        liq_rate[pool] = P_NOW
        print(f"  {pool}: liquidation quote failed, using spot")

rows = []
for (pool, tid), p in sorted(pos.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
    rf, ru = resid_fire[(pool, tid)]
    inv = rf * liq_rate[pool] + ru
    pnl = p["wd"] + p["fee"] + inv - p["dep"]
    # peak capital at risk
    run, peak = Decimal(0), Decimal(0)
    for blk, v in sorted(p["flows"]):
        run -= v
        if run > peak: peak = run
    roi_dep = (pnl / p["dep"] * 100) if p["dep"] > 0 else Decimal(0)
    roi_peak = (pnl / peak * 100) if peak > 0 else Decimal(0)
    rows.append({"pool": pool, "token_id": tid, "owner": p["owner"],
                 "capital_deployed_gross_usdg": str(p["dep"]),
                 "peak_capital_at_risk_usdg": str(peak),
                 "realized_withdrawals_usdg": str(p["wd"]),
                 "realized_fees_usdg": str(p["fee"]),
                 "residual_fire": str(rf), "residual_usdg": str(ru),
                 "inventory_liquidation_usdg": str(inv),
                 "net_pnl_usdg": str(pnl),
                 "roi_on_gross_pct": str(roi_dep),
                 "roi_on_peak_pct": str(roi_peak),
                 "first_seen": p["first"], "last_seen": p["last"]})

with open(OUT_POS, "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

op = defaultdict(lambda: defaultdict(Decimal))
opm = defaultdict(lambda: {"pos": 0, "pools": set()})
for r in rows:
    o = r["owner"]
    for k in ("capital_deployed_gross_usdg", "peak_capital_at_risk_usdg",
              "realized_withdrawals_usdg", "realized_fees_usdg",
              "inventory_liquidation_usdg", "net_pnl_usdg"):
        op[o][k] += Decimal(r[k])
    opm[o]["pos"] += 1; opm[o]["pools"].add(r["pool"])
oprows = []
for o, v in sorted(op.items(), key=lambda kv: -kv[1]["net_pnl_usdg"]):
    pk = v["peak_capital_at_risk_usdg"]
    oprows.append({"operator": o, "positions": opm[o]["pos"], "pools": "|".join(sorted(opm[o]["pools"])),
                   **{k: str(v[k]) for k in v},
                   "roi_on_peak_pct": str(v["net_pnl_usdg"] / pk * 100 if pk > 0 else 0)})
with open(OUT_OP, "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(oprows[0].keys())); wr.writeheader(); wr.writerows(oprows)

print("\n" + "=" * 78)
T = lambda rs, k: sum(Decimal(r[k]) for r in rs)
for pool in POOLS:
    rs = [r for r in rows if r["pool"] == pool]
    print(f"\n{pool}")
    print(f"  gross deployed (contemporaneous) {T(rs,'capital_deployed_gross_usdg'):>10.2f} USDG")
    print(f"  PEAK capital at risk             {T(rs,'peak_capital_at_risk_usdg'):>10.2f} USDG")
    print(f"  realized withdrawals             {T(rs,'realized_withdrawals_usdg'):>10.2f} USDG")
    print(f"  realized fees (pokes)            {T(rs,'realized_fees_usdg'):>10.2f} USDG")
    print(f"  inventory (liquidation-adjusted) {T(rs,'inventory_liquidation_usdg'):>10.2f} USDG")
    print(f"  NET P&L                          {T(rs,'net_pnl_usdg'):>10.2f} USDG")
    g, pk = T(rs, 'capital_deployed_gross_usdg'), T(rs, 'peak_capital_at_risk_usdg')
    print(f"  ROI on gross                     {(T(rs,'net_pnl_usdg')/g*100 if g>0 else 0):>9.1f}%")
    print(f"  ROI on peak capital              {(T(rs,'net_pnl_usdg')/pk*100 if pk>0 else 0):>9.1f}%")
allr = rows
print(f"\nCOMBINED  gross {T(allr,'capital_deployed_gross_usdg'):.2f}  "
      f"peak {T(allr,'peak_capital_at_risk_usdg'):.2f}  "
      f"fees {T(allr,'realized_fees_usdg'):.2f}  P&L {T(allr,'net_pnl_usdg'):.2f}")
fee_share = T(allr, 'realized_fees_usdg') / T(allr, 'net_pnl_usdg') * 100
print(f"fee share of P&L: {fee_share:.1f}%")
