"""Ranking-method sensitivity for the pre-trade Relay execution benchmark.

Builds the strictly-pre-trade candidate pool ONCE per order, persists it, then
scores every specification against that identical pool:

    time+size  : composite score = block distance/6000 + |ln(size ratio)|
    time-only  : block distance alone, size ignored

each at top-1 / median-3 / median-5. Because both ranking methods draw from the
same persisted pool, any difference is attributable to the ranking rule and not
to retrieval variation.

Every selected observation is recorded as tx#logIndex so each observational
price is independently reproducible.
"""
import csv, json, math, os, statistics, subprocess, time
from collections import Counter
from decimal import Decimal, getcontext
from eth_utils import keccak

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
INPUT = "data/analysis/FIRE_RELAY_ORDERS.csv"
POOL_CACHE = "data/analysis/_pretrade_candidate_pools.json"
OUTPUT = "data/analysis/FIRE_RELAY_BENCHMARK_RANKING_SENSITIVITY.csv"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
CANONICAL_POOL_ID = ("0x2276440d38b33394989f7819f63b1df5"
                     "ed62e48192706c172cabef1480547efd")
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_USDG_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"

V4_SWAP_TOPIC = "0x" + keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
V3_SWAP_TOPIC = "0x" + keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

WINDOWS = [36_000, 144_000]
CANON_CHUNK = 8_000
BRIDGE_CHUNK = 2_000
KS = [1, 3, 5]
METHODS = ["time+size", "time-only"]
FAILED = []


def rpc(method, params, tries=3):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last = None
    for a in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "40", RPC,
                                "-H", "Content-Type: application/json", "-d", payload],
                               capture_output=True, text=True, check=True)
            d = json.loads(p.stdout)
            if "error" in d:
                last = RuntimeError(d["error"]); time.sleep(0.4 * (a + 1)); continue
            return d["result"]
        except Exception as e:
            last = e; time.sleep(0.4 * (a + 1))
    raise last


def chunked_logs(address, topics, start, end, chunk):
    out, s = [], start
    while s <= end:
        e = min(s + chunk - 1, end)
        got = None
        for a in range(5):
            try:
                got = rpc("eth_getLogs", [{"address": address, "fromBlock": hex(s),
                                           "toBlock": hex(e), "topics": topics}], tries=1)
                break
            except Exception:
                time.sleep(0.3 * (a + 1))
        if got is None:
            FAILED.append([address, s, e])
        else:
            out += got
        s = e + 1
    return out


def sgn(v, bits): return v - (1 << bits) if v >= (1 << (bits - 1)) else v
def i128(w): return sgn(int(w, 16) & ((1 << 128) - 1), 128)
def i256(w): return sgn(int(w, 16), 256)


def call_addr(sel):
    return ("0x" + rpc("eth_call", [{"to": WETH_USDG_V3, "data": sel}, "latest"])[-40:]).lower()


token0 = call_addr("0x0dfe1681")
token1 = call_addr("0xd21220a7")
if USDG not in (token0, token1):
    raise RuntimeError("v3 pool does not contain USDG")
WETH = token1 if token0 == USDG else token0

with open(INPUT, newline="", encoding="utf-8") as f:
    orders = list(csv.DictReader(f))
targets = []
for r in orders:
    src = r["fire_source"].lower()
    if not src or r["relay_depositor"].lower() != src or r["deposit_token"].lower() != USDG:
        continue
    targets.append({**r,
                    "fire": Decimal(r["fire_sold"]),
                    "actual": Decimal(r["deposit_amount_raw"]) / Decimal(10 ** 6)})
print(f"orders: {len(targets)}")


def fetch_canonical(blk):
    for w in WINDOWS:
        logs = chunked_logs(POOL_MANAGER, [V4_SWAP_TOPIC, CANONICAL_POOL_ID],
                            max(0, blk - w), blk - 1, CANON_CHUNK)
        out = []
        for lg in logs:
            d = lg["data"][2:]
            ws = [d[i:i + 64] for i in range(0, len(d), 64)]
            a0, a1 = i128(ws[0]), i128(ws[1])
            if a1 >= 0 or a0 <= 0:
                continue
            fi = Decimal(-a1) / Decimal(10 ** 18)
            eo = Decimal(a0) / Decimal(10 ** 18)
            if fi <= 0 or eo <= 0:
                continue
            out.append({"block": int(lg["blockNumber"], 16), "size": str(fi),
                        "rate": str(eo / fi), "tx": lg["transactionHash"],
                        "log_index": int(lg["logIndex"], 16)})
        if out:
            return out
    return []


def fetch_bridge(blk):
    for w in WINDOWS:
        logs = chunked_logs(WETH_USDG_V3, [V3_SWAP_TOPIC],
                            max(0, blk - w), blk - 1, BRIDGE_CHUNK)
        out = []
        for lg in logs:
            d = lg["data"][2:]
            ws = [d[i:i + 64] for i in range(0, len(d), 64)]
            amts = {token0: i256(ws[0]), token1: i256(ws[1])}
            we, us = amts[WETH], amts[USDG]
            if we <= 0 or us >= 0:
                continue
            wi = Decimal(we) / Decimal(10 ** 18)
            uo = Decimal(-us) / Decimal(10 ** 6)
            if wi <= 0 or uo <= 0:
                continue
            out.append({"block": int(lg["blockNumber"], 16), "size": str(wi),
                        "rate": str(uo / wi), "tx": lg["transactionHash"],
                        "log_index": int(lg["logIndex"], 16)})
        if out:
            return out
    return []


# ---- build / load persisted pre-trade candidate pools -------------------
if os.path.exists(POOL_CACHE):
    pools = json.load(open(POOL_CACHE))
    print(f"loaded persisted candidate pools ({len(pools)} entries)")
else:
    pools = {}

for i, t in enumerate(targets, 1):
    rc = rpc("eth_getTransactionReceipt", [t["origin_tx"]])
    t["block"] = int(rc["blockNumber"], 16)
    key = t["origin_tx"]
    if key not in pools:
        pools[key] = {"block": t["block"],
                      "canonical": fetch_canonical(t["block"]),
                      "bridge": fetch_bridge(t["block"])}
        json.dump(pools, open(POOL_CACHE, "w"))
        print(f"[{i:02}/{len(targets)}] pool built: "
              f"{len(pools[key]['canonical'])} canonical, {len(pools[key]['bridge'])} bridge")

# ---- scoring ------------------------------------------------------------
def rank(cands, blk, size, method):
    def key(c):
        t = Decimal(abs(c["block"] - blk)) / Decimal(6000)
        if method == "time-only":
            return t
        s = Decimal(c["size"])
        if s <= 0 or size <= 0:
            return t + Decimal(100)
        return t + Decimal(abs(math.log(float(s / size))))
    return sorted(cands, key=key)


def combine(ranked, k, blk, size):
    sel = ranked[:k]
    if not sel:
        return None
    rate = Decimal(str(statistics.median([float(Decimal(c["rate"])) for c in sel])))
    return {"rate": rate, "sel": sel,
            "blocks_back": min(blk - c["block"] for c in sel),
            "ratio": min(max(Decimal(c["size"]) / size, size / Decimal(c["size"])) for c in sel)}


def grade(c, b):
    if c["blocks_back"] <= 3000 and b["blocks_back"] <= 3000 and c["ratio"] <= 2 and b["ratio"] <= 2:
        return "HIGH"
    if c["blocks_back"] <= 18000 and b["blocks_back"] <= 18000 and c["ratio"] <= 4 and b["ratio"] <= 4:
        return "MEDIUM"
    return "LOW"


rows = []
for t in targets:
    p = pools[t["origin_tx"]]
    blk, fire, actual = t["block"], t["fire"], t["actual"]
    row = {"origin_tx": t["origin_tx"], "order_id": t["order_id"],
           "fire_source": t["fire_source"], "block": blk,
           "fire_sold": str(fire), "actual_relay_deposit_usdg": str(actual),
           "canonical_pool_size": len(p["canonical"]), "bridge_pool_size": len(p["bridge"])}
    ok = True
    for m in METHODS:
        cr = rank(p["canonical"], blk, fire, m)
        for k in KS:
            tag = f"{'ts' if m == 'time+size' else 'to'}{k}"
            c = combine(cr, k, blk, fire)
            if not c:
                ok = False; break
            eth = fire * c["rate"]
            br = rank(p["bridge"], blk, eth, m)
            b = combine(br, k, blk, eth)
            if not b:
                ok = False; break
            bench = eth * b["rate"]
            row[f"{tag}_benchmark_usdg"] = str(bench)
            row[f"{tag}_diff_pct"] = str((actual - bench) / bench * 100)
            row[f"{tag}_confidence"] = grade(c, b)
            row[f"{tag}_canonical_obs"] = "|".join(f"{o['tx']}#{o['log_index']}" for o in c["sel"])
            row[f"{tag}_bridge_obs"] = "|".join(f"{o['tx']}#{o['log_index']}" for o in b["sel"])
        if not ok:
            break
    if ok:
        rows.append(row)

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f"\nWrote {OUTPUT} ({len(rows)} rows)")
if FAILED:
    print(f"WARNING: {len(FAILED)} log chunks failed after retries")

print()
print("=" * 74)
print("RANKING SENSITIVITY  (strictly pre-trade candidates, identical pools)")
print("=" * 74)


def table(rs, label):
    print(f"\n{label}")
    print(f"{'spec':<26}{'worse/N':>12}{'median':>12}{'vol-weighted':>16}")
    for m in METHODS:
        for k in KS:
            tag = f"{'ts' if m == 'time+size' else 'to'}{k}"
            sub = [r for r in rs if r.get(f"{tag}_diff_pct")]
            if label.startswith("HIGH"):
                sub = [r for r in sub if r[f"{tag}_confidence"] in ("HIGH", "MEDIUM")]
            if not sub:
                print(f"{m+', '+str(k):<26}{'--':>12}"); continue
            d = [Decimal(r[f"{tag}_diff_pct"]) for r in sub]
            worse = sum(1 for x in d if x < 0)
            med = statistics.median([float(x) for x in d])
            a = sum(Decimal(r["actual_relay_deposit_usdg"]) for r in sub)
            bb = sum(Decimal(r[f"{tag}_benchmark_usdg"]) for r in sub)
            vw = (a - bb) / bb * 100
            name = f"{m}, {'top' if k == 1 else 'median'} {k}"
            print(f"{name:<26}{f'{worse} / {len(sub)}':>12}{med:>11.3f}%{vw:>15.2f}%")


table(rows, "ALL ORDERS")
table(rows, "HIGH/MEDIUM-quality matches only")
print("\nConfidence mix:")
for m in METHODS:
    for k in KS:
        tag = f"{'ts' if m == 'time+size' else 'to'}{k}"
        c = Counter(r[f"{tag}_confidence"] for r in rows)
        print(f"  {m:<11} k={k}  HIGH={c['HIGH']:2} MEDIUM={c['MEDIUM']:2} LOW={c['LOW']:2}")
