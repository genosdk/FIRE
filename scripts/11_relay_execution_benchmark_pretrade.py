"""Relay origin-side execution benchmark, PRE-TRADE ONLY.

Differences from 09:
  * candidate observations are restricted to  candidate_block < target_block
    (no look-ahead; 09 allowed a symmetric window around the trade)
  * three specifications from the same pre-trade pool: nearest-1, median-3, median-5
  * bridge logs fetched in much narrower chunks (the busy v3 pool timed out in 09)
  * each selected observation is recorded as tx#logIndex, so the exact swap that
    entered the benchmark is reproducible (09 stored tx hashes only, which is
    ambiguous when a tx contains several swaps on the same pool)

"Nearest" means lowest composite match score (block distance + |log size ratio|),
the same ranking 09 used; the specs differ only in how many of the top-ranked
observations are combined.
"""
import csv, json, math, statistics, subprocess, time
from decimal import Decimal, getcontext
from eth_utils import keccak

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
INPUT = "data/analysis/FIRE_RELAY_ORDERS.csv"
OUTPUT = "data/analysis/FIRE_RELAY_EXECUTION_BENCHMARK_PRETRADE.csv"

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
SPECS = [("nearest1", 1), ("median3", 3), ("median5", 5)]


def rpc(method, params, tries=3):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    last = None
    for attempt in range(tries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "40", RPC,
                                "-H", "Content-Type: application/json", "-d", payload],
                               capture_output=True, text=True, check=True)
            data = json.loads(p.stdout)
            if "error" in data:
                last = RuntimeError(f"{method}: {data['error']}")
                time.sleep(0.4 * (attempt + 1)); continue
            return data["result"]
        except Exception as e:
            last = e; time.sleep(0.4 * (attempt + 1))
    raise last


FAILED_CHUNKS = []


def chunked_logs(address, topics, start, end, chunk):
    out, s = [], start
    while s <= end:
        e = min(s + chunk - 1, end)
        got = None
        for attempt in range(5):
            try:
                got = rpc("eth_getLogs", [{"address": address, "fromBlock": hex(s),
                                           "toBlock": hex(e), "topics": topics}], tries=1)
                break
            except Exception:
                time.sleep(0.3 * (attempt + 1))
        if got is None:
            FAILED_CHUNKS.append((address, s, e))
        else:
            out += got
        s = e + 1
    return out


def signed(v, bits):
    return v - (1 << bits) if v >= (1 << (bits - 1)) else v


def int128(w): return signed(int(w, 16) & ((1 << 128) - 1), 128)
def int256(w): return signed(int(w, 16), 256)


def call_addr(sel):
    return ("0x" + rpc("eth_call", [{"to": WETH_USDG_V3, "data": sel}, "latest"])[-40:]).lower()


token0 = call_addr("0x0dfe1681")
token1 = call_addr("0xd21220a7")
if USDG not in (token0, token1):
    raise RuntimeError("v3 pool does not contain USDG")
WETH = token1 if token0 == USDG else token0
print(f"bridge pool token0={token0} token1={token1} WETH={WETH}\n")

with open(INPUT, newline="", encoding="utf-8") as f:
    relay_rows = list(csv.DictReader(f))

targets = []
for r in relay_rows:
    src = r["fire_source"].lower(); dep = r["relay_depositor"].lower()
    if not src or dep != src or r["deposit_token"].lower() != USDG:
        continue
    targets.append({**r,
                    "fire_sold_dec": Decimal(r["fire_sold"]),
                    "deposit_usdg_dec": Decimal(r["deposit_amount_raw"]) / Decimal(10 ** 6)})
print(f"FIRE-relevant Relay orders: {len(targets)}")

for r in targets:
    rc = rpc("eth_getTransactionReceipt", [r["origin_tx"]])
    r["block"] = int(rc["blockNumber"], 16)
print("blocks resolved\n")

canon_cache, bridge_cache = {}, {}


def canonical_candidates(target_block, window):
    key = (target_block, window)
    if key in canon_cache: return canon_cache[key]
    start = max(0, target_block - window)
    end = target_block - 1                      # STRICTLY PRE-TRADE
    logs = chunked_logs(POOL_MANAGER, [V4_SWAP_TOPIC, CANONICAL_POOL_ID],
                        start, end, CANON_CHUNK)
    out = []
    for lg in logs:
        d = lg["data"][2:]
        w = [d[i:i + 64] for i in range(0, len(d), 64)]
        a0, a1 = int128(w[0]), int128(w[1])     # a0 = native ETH, a1 = FIRE
        if a1 >= 0 or a0 <= 0:                  # need FIRE in, ETH out (caller-centric)
            continue
        fire_in = Decimal(-a1) / Decimal(10 ** 18)
        eth_out = Decimal(a0) / Decimal(10 ** 18)
        if fire_in <= 0 or eth_out <= 0: continue
        out.append({"block": int(lg["blockNumber"], 16), "size": fire_in,
                    "rate": eth_out / fire_in, "tx": lg["transactionHash"],
                    "log_index": int(lg["logIndex"], 16)})
    canon_cache[key] = out
    return out


def bridge_candidates(target_block, window):
    key = (target_block, window)
    if key in bridge_cache: return bridge_cache[key]
    start = max(0, target_block - window)
    end = target_block - 1                      # STRICTLY PRE-TRADE
    logs = chunked_logs(WETH_USDG_V3, [V3_SWAP_TOPIC], start, end, BRIDGE_CHUNK)
    out = []
    for lg in logs:
        d = lg["data"][2:]
        w = [d[i:i + 64] for i in range(0, len(d), 64)]
        amts = {token0: int256(w[0]), token1: int256(w[1])}
        we, us = amts[WETH], amts[USDG]
        if we <= 0 or us >= 0:                  # need WETH in, USDG out (pool-centric)
            continue
        weth_in = Decimal(we) / Decimal(10 ** 18)
        usdg_out = Decimal(-us) / Decimal(10 ** 6)
        if weth_in <= 0 or usdg_out <= 0: continue
        out.append({"block": int(lg["blockNumber"], 16), "size": weth_in,
                    "rate": usdg_out / weth_in, "tx": lg["transactionHash"],
                    "log_index": int(lg["logIndex"], 16)})
    bridge_cache[key] = out
    return out


def score(c, target_block, target_size):
    t = abs(c["block"] - target_block) / Decimal(6000)
    if c["size"] <= 0 or target_size <= 0:
        s = Decimal(100)
    else:
        s = Decimal(abs(math.log(float(c["size"] / target_size))))
    return t + s


def rank(fetch, target_block, target_size):
    for window in WINDOWS:
        cands = fetch(target_block, window)
        if cands:
            return sorted(cands, key=lambda c: score(c, target_block, target_size))
    return []


def combine(ranked, k, target_block, target_size):
    sel = ranked[:k]
    if not sel: return None
    rate = Decimal(str(statistics.median([float(c["rate"]) for c in sel])))
    return {"rate": rate, "sel": sel,
            "nearest_blocks": min(target_block - c["block"] for c in sel),
            "best_ratio": min(max(c["size"] / target_size, target_size / c["size"]) for c in sel)}


def grade(c, b):
    if (c["nearest_blocks"] <= 3000 and b["nearest_blocks"] <= 3000
            and c["best_ratio"] <= 2 and b["best_ratio"] <= 2):
        return "HIGH"
    if (c["nearest_blocks"] <= 18000 and b["nearest_blocks"] <= 18000
            and c["best_ratio"] <= 4 and b["best_ratio"] <= 4):
        return "MEDIUM"
    return "LOW"


rows = []
for i, r in enumerate(targets, 1):
    blk = r["block"]; fire = r["fire_sold_dec"]; actual = r["deposit_usdg_dec"]
    cr = rank(canonical_candidates, blk, fire)
    if not cr:
        print(f"[{i:02}] {r['origin_tx'][:14]}… NO PRE-TRADE CANONICAL"); continue
    row = {"origin_tx": r["origin_tx"], "order_id": r["order_id"],
           "fire_source": r["fire_source"], "block": blk, "fire_sold": str(fire),
           "actual_relay_deposit_usdg": str(actual),
           "actual_usdg_per_fire": str(actual / fire)}
    ok = True
    for name, k in SPECS:
        c = combine(cr, k, blk, fire)
        synth_eth = fire * c["rate"]
        br = rank(bridge_candidates, blk, synth_eth)
        if not br:
            print(f"[{i:02}] {r['origin_tx'][:14]}… NO PRE-TRADE BRIDGE ({name})")
            ok = False; break
        b = combine(br, k, blk, synth_eth)
        bench = synth_eth * b["rate"]
        row[f"{name}_canonical_eth_per_fire"] = str(c["rate"])
        row[f"{name}_bridge_usdg_per_eth"] = str(b["rate"])
        row[f"{name}_benchmark_usdg"] = str(bench)
        row[f"{name}_diff_usdg"] = str(actual - bench)
        row[f"{name}_diff_pct"] = str((actual - bench) / bench * 100)
        row[f"{name}_confidence"] = grade(c, b)
        row[f"{name}_canonical_blocks_back"] = c["nearest_blocks"]
        row[f"{name}_bridge_blocks_back"] = b["nearest_blocks"]
        row[f"{name}_canonical_size_ratio"] = str(c["best_ratio"])
        row[f"{name}_bridge_size_ratio"] = str(b["best_ratio"])
        row[f"{name}_canonical_obs"] = "|".join(f"{o['tx']}#{o['log_index']}" for o in c["sel"])
        row[f"{name}_bridge_obs"] = "|".join(f"{o['tx']}#{o['log_index']}" for o in b["sel"])
    if not ok: continue
    rows.append(row)
    print(f"[{i:02}/{len(targets)}] FIRE={fire:>10,.0f}  "
          f"n1={Decimal(row['nearest1_diff_pct']):+7.2f}%({row['nearest1_confidence'][:1]})  "
          f"m3={Decimal(row['median3_diff_pct']):+7.2f}%({row['median3_confidence'][:1]})  "
          f"m5={Decimal(row['median5_diff_pct']):+7.2f}%({row['median5_confidence'][:1]})")

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)

print(f"\nWrote {OUTPUT}  ({len(rows)} rows)")
if FAILED_CHUNKS:
    print(f"WARNING: {len(FAILED_CHUNKS)} log chunks failed after retries")

print()
print("=" * 78)
print("ROBUSTNESS MATRIX  (pre-trade observations only)")
print("=" * 78)


def report(subset, label):
    print(f"\n{label}")
    print(f"{'spec':<12}{'N worse':>12}{'median':>12}{'volume-weighted':>20}")
    for name, _ in SPECS:
        rs = [r for r in rows if r in subset[name]]
        if not rs:
            print(f"{name:<12}{'--':>12}"); continue
        diffs = [Decimal(r[f"{name}_diff_pct"]) for r in rs]
        worse = sum(1 for d in diffs if d < 0)
        med = statistics.median([float(d) for d in diffs])
        a = sum(Decimal(r["actual_relay_deposit_usdg"]) for r in rs)
        b = sum(Decimal(r[f"{name}_benchmark_usdg"]) for r in rs)
        vw = (a - b) / b * 100
        print(f"{name:<12}{f'{worse} / {len(rs)}':>12}{med:>11.3f}%{vw:>19.2f}%")


allsub = {n: rows for n, _ in SPECS}
hmsub = {n: [r for r in rows if r[f"{n}_confidence"] in ("HIGH", "MEDIUM")] for n, _ in SPECS}
report(allsub, "ALL ORDERS")
report(hmsub, "HIGH/MEDIUM-quality matches only")
print("\nConfidence mix per spec:")
for name, _ in SPECS:
    from collections import Counter
    c = Counter(r[f"{name}_confidence"] for r in rows)
    print(f"  {name:<10} HIGH={c['HIGH']:2}  MEDIUM={c['MEDIUM']:2}  LOW={c['LOW']:2}")
