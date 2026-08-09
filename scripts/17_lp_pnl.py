"""Lifetime P&L for every LP position in the 40% and 50% FIRE/USDG pools.

For each ModifyLiquidity transaction we compute the position owner's NET token
delta (FIRE and USDG) from the receipt's ERC-20 Transfer logs. That captures
deposits, withdrawals and realized fee collections uniformly, without needing
to distinguish them -- a poke that realizes fees shows up as a positive delta,
a deposit as a negative one.

Residual inventory is derived from each position's net liquidity and the pool's
current sqrtPrice, using the standard full-range amount formulas. Everything is
then marked to the canonical FIRE market price obtained live from the v4 Quoter.

    net P&L (USDG) = (realized_FIRE + residual_FIRE) * P_canonical
                   + (realized_USDG + residual_USDG)
"""
import csv, json, subprocess, time
from collections import defaultdict
from decimal import Decimal, getcontext
from eth_abi import encode, decode
from eth_utils import keccak

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSM = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
XFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
CANON = "0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"

POOLS = {"FIRE_USDG_50pct": "0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43",
         "FIRE_USDG_40pct": "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636"}
LIQ = "data/liquidity/FIRE_USDG_ALL_modify_liquidity.csv"
OUT_POS = "data/analysis/LP_PNL_BY_POSITION.csv"
OUT_OP = "data/analysis/LP_PNL_BY_OPERATOR.csv"

SEL = keccak(text="quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")[:4]
ARG = "((address,address,uint24,int24,address),bool,uint128,bytes)"


def rpc(m, p, tries=4):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "40", RPC,
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


# ---- canonical FIRE price (USDG per FIRE), live ------------------------
lg = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest", "topics": [INIT, CANON]}])[0]
w = [lg["data"][2:][i:i + 64] for i in range(0, len(lg["data"][2:]), 64)]
CK = (NATIVE, FIRE, int(w[0], 16) & 0xFFFFFF, i24(w[1]), "0x" + w[2][24:])
nat_t = "0x" + "00" * 32
usdg_t = "0x" + "00" * 12 + USDG[2:]
best, bkey = 0, None
for L in rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                              "topics": [INIT, None, nat_t, usdg_t]}]):
    d = L["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
    fee = int(ws[0], 16) & 0xFFFFFF
    if fee > 3000: continue
    k = (NATIVE, USDG, fee, i24(ws[1]), "0x" + ws[2][24:])
    o = quote(k, True, 10 ** 16)
    if o and o > best: best, bkey = o, k
probe = 1000 * 10 ** 18
eth = quote(CK, False, probe)
usdg = quote(bkey, True, eth)
P_FIRE = Decimal(usdg) / Decimal(10 ** 6) / Decimal(1000)
print(f"canonical FIRE price: {P_FIRE:.12f} USDG/FIRE  (via fee={CK[2]} then fee={bkey[2]})\n")

# ---- current sqrtPrice per pool (last Swap) ----------------------------
def s128(x):
    v = int(x, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >= 1 << 127 else v

sqrtP = {}
latest = int(rpc("eth_blockNumber", []), 16)
for name, pid in POOLS.items():
    last = None; s = 13_400_000
    while s <= latest:
        e = min(s + 400_000, latest)
        try:
            lgs = rpc("eth_getLogs", [{"address": PM, "fromBlock": hex(s), "toBlock": hex(e),
                                       "topics": [SWAP, pid]}], tries=2)
            if lgs: last = lgs[-1]
        except Exception:
            pass
        s = e + 1
    d = last["data"][2:]; ws = [d[i:i + 64] for i in range(0, len(d), 64)]
    sqrtP[name] = Decimal(int(ws[2], 16)) / Decimal(2 ** 96)
    print(f"{name}: current sqrtPrice ratio {sqrtP[name]:.6e}  "
          f"(price {(sqrtP[name]**2)*Decimal(10**12):.12f} USDG/FIRE)")

# full-range bounds used by every position in these pools
TA, TB = Decimal(-880000), Decimal(880000)
def sqrt_at(tick):
    return Decimal(1.0001) ** (tick / 2)
SA, SB = sqrt_at(TA), sqrt_at(TB)

# ---- walk every LP transaction ----------------------------------------
liq = list(csv.DictReader(open(LIQ)))
txs = sorted({r["transaction_hash"] for r in liq})
print(f"\nLP transactions to scan: {len(txs)}")
owner_of_tx = {}
for r in liq:
    owner_of_tx[r["transaction_hash"]] = r["tx_from"].lower()

delta = defaultdict(lambda: {"fire": Decimal(0), "usdg": Decimal(0)})
for i, tx in enumerate(txs, 1):
    rc = rpc("eth_getTransactionReceipt", [tx])
    own = owner_of_tx[tx]
    for l in rc["logs"]:
        if not l["topics"] or l["topics"][0].lower() != XFER or len(l["topics"]) < 3:
            continue
        a = l["address"].lower()
        if a not in (FIRE, USDG):
            continue
        frm = ("0x" + l["topics"][1][26:]).lower()
        to = ("0x" + l["topics"][2][26:]).lower()
        v = Decimal(int(l["data"], 16)) / Decimal(10 ** (18 if a == FIRE else 6))
        k = "fire" if a == FIRE else "usdg"
        if to == own: delta[tx][k] += v
        if frm == own: delta[tx][k] -= v
    if i % 40 == 0 or i == len(txs):
        print(f"  scanned {i}/{len(txs)}")

# ---- aggregate per position and per operator --------------------------
pos = defaultdict(lambda: {"pool": "", "owner": "", "liq": 0,
                           "fire": Decimal(0), "usdg": Decimal(0),
                           "adds": 0, "removes": 0, "zeros": 0, "events": 0,
                           "first": "", "last": ""})
seen_tx = set()
for r in liq:
    key = (r["pool"], r["possible_token_id"])
    p = pos[key]
    p["pool"] = r["pool"]; p["owner"] = r["tx_from"].lower()
    p["liq"] += int(r["liquidity_delta"]); p["events"] += 1
    p["adds"] += r["action"] == "ADD"; p["removes"] += r["action"] == "REMOVE"
    p["zeros"] += r["action"] == "ZERO"
    if not p["first"]: p["first"] = r["timestamp_utc"]
    p["last"] = r["timestamp_utc"]
    tk = (r["transaction_hash"], key)
    if tk not in seen_tx:
        seen_tx.add(tk)
        p["fire"] += delta[r["transaction_hash"]]["fire"]
        p["usdg"] += delta[r["transaction_hash"]]["usdg"]

rows = []
for (pool, tid), p in sorted(pos.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
    L = Decimal(p["liq"])
    S = sqrtP[pool]
    res_fire = L * (Decimal(1) / S - Decimal(1) / SB) / Decimal(10 ** 18) if L > 0 else Decimal(0)
    res_usdg = L * (S - SA) / Decimal(10 ** 6) if L > 0 else Decimal(0)
    pnl = (p["fire"] + res_fire) * P_FIRE + (p["usdg"] + res_usdg)
    invested = max(Decimal(0), -(p["fire"] * P_FIRE + p["usdg"]))
    roi = (pnl / invested * 100) if invested > 0 else Decimal(0)
    rows.append({"pool": pool, "token_id": tid, "owner": p["owner"],
                 "events": p["events"], "adds": p["adds"], "removes": p["removes"], "zeros": p["zeros"],
                 "net_liquidity": p["liq"],
                 "realized_fire": str(p["fire"]), "realized_usdg": str(p["usdg"]),
                 "residual_fire": str(res_fire), "residual_usdg": str(res_usdg),
                 "capital_deployed_usdg": str(invested),
                 "net_pnl_usdg": str(pnl), "roi_pct": str(roi),
                 "first_seen": p["first"], "last_seen": p["last"]})

with open(OUT_POS, "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys())); wr.writeheader(); wr.writerows(rows)

op = defaultdict(lambda: defaultdict(Decimal))
opmeta = defaultdict(lambda: {"positions": 0, "pools": set(), "events": 0})
for r in rows:
    o = r["owner"]
    for k in ("realized_fire", "realized_usdg", "residual_fire", "residual_usdg",
              "capital_deployed_usdg", "net_pnl_usdg"):
        op[o][k] += Decimal(r[k])
    opmeta[o]["positions"] += 1; opmeta[o]["pools"].add(r["pool"]); opmeta[o]["events"] += int(r["events"])
oprows = []
for o, v in sorted(op.items(), key=lambda kv: -kv[1]["net_pnl_usdg"]):
    cap = v["capital_deployed_usdg"]
    oprows.append({"operator": o, "positions": opmeta[o]["positions"],
                   "pools": "|".join(sorted(opmeta[o]["pools"])), "events": opmeta[o]["events"],
                   "realized_fire": str(v["realized_fire"]), "realized_usdg": str(v["realized_usdg"]),
                   "residual_fire": str(v["residual_fire"]), "residual_usdg": str(v["residual_usdg"]),
                   "capital_deployed_usdg": str(cap), "net_pnl_usdg": str(v["net_pnl_usdg"]),
                   "roi_pct": str(v["net_pnl_usdg"] / cap * 100 if cap > 0 else 0)})
with open(OUT_OP, "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(oprows[0].keys())); wr.writeheader(); wr.writerows(oprows)

print(f"\nWrote {OUT_POS} and {OUT_OP}")
print("\n" + "=" * 78)
for pool in POOLS:
    rs = [r for r in rows if r["pool"] == pool]
    tot = lambda k: sum(Decimal(r[k]) for r in rs)
    print(f"\n{pool}")
    print(f"  positions            {len(rs)}")
    print(f"  operators            {len(set(r['owner'] for r in rs))}")
    print(f"  capital deployed     {tot('capital_deployed_usdg'):>14.6f} USDG-equiv")
    print(f"  realized FIRE        {tot('realized_fire'):>14.4f}")
    print(f"  realized USDG        {tot('realized_usdg'):>14.6f}")
    print(f"  residual FIRE        {tot('residual_fire'):>14.4f}")
    print(f"  residual USDG        {tot('residual_usdg'):>14.6f}")
    print(f"  NET P&L              {tot('net_pnl_usdg'):>14.6f} USDG")
    cap = tot('capital_deployed_usdg')
    print(f"  ROI                  {(tot('net_pnl_usdg')/cap*100 if cap>0 else 0):>13.2f}%")
print("\nTop operators by net P&L:")
for r in oprows[:8]:
    print(f"  {r['operator']}  pos={r['positions']:2}  cap={Decimal(r['capital_deployed_usdg']):>9.4f}  "
          f"pnl={Decimal(r['net_pnl_usdg']):>+9.4f}  roi={Decimal(r['roi_pct']):>+8.2f}%  {r['pools']}")
