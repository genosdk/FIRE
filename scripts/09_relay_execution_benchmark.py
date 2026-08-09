import csv
import json
import math
import statistics
import subprocess
import time
from collections import defaultdict
from decimal import Decimal, getcontext

from eth_utils import keccak

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
INPUT = "FIRE_RELAY_ORDERS.csv"
OUTPUT = "FIRE_RELAY_EXECUTION_BENCHMARK.csv"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
CANONICAL_POOL_ID = ("0x2276440d38b33394989f7819f63b1df5" "ed62e48192706c172cabef1480547efd")
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_USDG_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"

V4_SWAP_TOPIC = "0x" + keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
V3_SWAP_TOPIC = "0x" + keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

WINDOWS = [36_000, 144_000]
TOP_K = 5


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    p = subprocess.run(["curl", "-sS", "--max-time", "40", RPC,
                        "-H", "Content-Type: application/json", "-d", payload],
                       capture_output=True, text=True, check=True)
    data = json.loads(p.stdout)
    if "error" in data:
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


def chunked_logs(address, topics, start, end, chunk=8000):
    """eth_getLogs in sub-ranges; this RPC times out on wide windows for busy pools."""
    out = []
    s = start
    while s <= end:
        e = min(s + chunk - 1, end)
        got = None
        for attempt in range(4):
            try:
                got = rpc("eth_getLogs", [{"address": address, "fromBlock": hex(s),
                                           "toBlock": hex(e), "topics": topics}])
                break
            except Exception:
                time.sleep(0.4 * (attempt + 1))
        if got is None:
            print(f"    WARN: chunk {s}-{e} failed after retries on {address[:12]}")
        else:
            out += got
        s = e + 1
    return out


def signed(value, bits):
    if value >= (1 << (bits - 1)):
        value -= (1 << bits)
    return value


def int128(word):
    return signed(int(word, 16) & ((1 << 128) - 1), 128)


def int256(word):
    return signed(int(word, 16), 256)


def contract_address(selector):
    result = rpc("eth_call", [{"to": WETH_USDG_V3, "data": selector}, "latest"])
    return ("0x" + result[-40:]).lower()


token0 = contract_address("0x0dfe1681")
token1 = contract_address("0xd21220a7")

if USDG not in (token0, token1):
    raise RuntimeError("Configured v3 pool does not contain USDG.")

WETH = token1 if token0 == USDG else token0

print("WETH/USDG v3 verification")
print("token0:", token0); print("token1:", token1)
print("WETH:  ", WETH); print("USDG:  ", USDG)
print("V3 swap topic:", V3_SWAP_TOPIC)
print()

with open(INPUT, newline="", encoding="utf-8") as f:
    relay_rows = list(csv.DictReader(f))

targets = []
for r in relay_rows:
    fire_source = r["fire_source"].lower()
    depositor = r["relay_depositor"].lower()
    if not fire_source or depositor != fire_source:
        continue
    if r["deposit_token"].lower() != USDG:
        continue
    fire_sold = Decimal(r["fire_sold"])
    deposit_usdg = Decimal(r["deposit_amount_raw"]) / Decimal(10 ** 6)
    targets.append({**r, "fire_sold_dec": fire_sold, "deposit_usdg_dec": deposit_usdg})

print(f"FIRE-relevant Relay orders: {len(targets)}")
if len(targets) != 20:
    print("WARNING: expected 20 based on prior reconciliation.")

for i, r in enumerate(targets, 1):
    receipt = rpc("eth_getTransactionReceipt", [r["origin_tx"]])
    r["block"] = int(receipt["blockNumber"], 16)

print(f"Resolved {len(targets)} blocks\n")

canonical_cache = {}
bridge_cache = {}


def get_canonical_candidates(target_block, window):
    key = (target_block, window)
    if key in canonical_cache:
        return canonical_cache[key]
    start = max(0, target_block - window); end = target_block + window
    logs = chunked_logs(POOL_MANAGER, [V4_SWAP_TOPIC, CANONICAL_POOL_ID], start, end)
    candidates = []
    for log in logs:
        raw = log["data"][2:]
        words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
        amount0 = int128(words[0])
        amount1 = int128(words[1])
        if amount1 >= 0 or amount0 <= 0:
            continue
        fire_in = Decimal(-amount1) / Decimal(10 ** 18)
        eth_out = Decimal(amount0) / Decimal(10 ** 18)
        if fire_in <= 0 or eth_out <= 0:
            continue
        candidates.append({"block": int(log["blockNumber"], 16), "fire_in": fire_in,
                           "eth_out": eth_out, "eth_per_fire": eth_out / fire_in,
                           "tx": log["transactionHash"]})
    canonical_cache[key] = candidates
    return candidates


def get_bridge_candidates(target_block, window):
    key = (target_block, window)
    if key in bridge_cache:
        return bridge_cache[key]
    start = max(0, target_block - window); end = target_block + window
    logs = chunked_logs(WETH_USDG_V3, [V3_SWAP_TOPIC], start, end)
    candidates = []
    for log in logs:
        raw = log["data"][2:]
        words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
        amount0 = int256(words[0]); amount1 = int256(words[1])
        amounts = {token0: amount0, token1: amount1}
        weth_raw = amounts[WETH]; usdg_raw = amounts[USDG]
        if weth_raw <= 0 or usdg_raw >= 0:
            continue
        weth_in = Decimal(weth_raw) / Decimal(10 ** 18)
        usdg_out = Decimal(-usdg_raw) / Decimal(10 ** 6)
        if weth_in <= 0 or usdg_out <= 0:
            continue
        candidates.append({"block": int(log["blockNumber"], 16), "weth_in": weth_in,
                           "usdg_out": usdg_out, "usdg_per_eth": usdg_out / weth_in,
                           "tx": log["transactionHash"]})
    bridge_cache[key] = candidates
    return candidates


def score_candidate(candidate_block, target_block, candidate_size, target_size):
    time_score = abs(candidate_block - target_block) / Decimal(6000)
    if candidate_size <= 0 or target_size <= 0:
        size_score = Decimal(100)
    else:
        size_score = Decimal(abs(math.log(float(candidate_size / target_size))))
    return time_score + size_score


def select_canonical(target_block, target_fire):
    for window in WINDOWS:
        candidates = get_canonical_candidates(target_block, window)
        ranked = sorted(candidates, key=lambda c: score_candidate(
            c["block"], target_block, c["fire_in"], target_fire))
        if ranked:
            selected = ranked[:TOP_K]
            break
    else:
        return None
    rates = [c["eth_per_fire"] for c in selected]
    rate = Decimal(str(statistics.median([float(x) for x in rates])))
    nearest_blocks = min(abs(c["block"] - target_block) for c in selected)
    best_size_ratio = min(max(c["fire_in"] / target_fire, target_fire / c["fire_in"])
                          for c in selected)
    return {"rate": rate, "selected": selected, "nearest_blocks": nearest_blocks,
            "best_size_ratio": best_size_ratio}


def select_bridge(target_block, target_eth):
    for window in WINDOWS:
        candidates = get_bridge_candidates(target_block, window)
        ranked = sorted(candidates, key=lambda c: score_candidate(
            c["block"], target_block, c["weth_in"], target_eth))
        if ranked:
            selected = ranked[:TOP_K]
            break
    else:
        return None
    rates = [c["usdg_per_eth"] for c in selected]
    rate = Decimal(str(statistics.median([float(x) for x in rates])))
    nearest_blocks = min(abs(c["block"] - target_block) for c in selected)
    best_size_ratio = min(max(c["weth_in"] / target_eth, target_eth / c["weth_in"])
                          for c in selected)
    return {"rate": rate, "selected": selected, "nearest_blocks": nearest_blocks,
            "best_size_ratio": best_size_ratio}


output = []
for i, r in enumerate(targets, 1):
    block = r["block"]; fire_sold = r["fire_sold_dec"]; actual_usdg = r["deposit_usdg_dec"]
    actual_rate = actual_usdg / fire_sold
    canonical = select_canonical(block, fire_sold)
    if not canonical:
        print(f"{r['origin_tx']}: NO CANONICAL MATCH"); continue
    synthetic_eth = fire_sold * canonical["rate"]
    bridge = select_bridge(block, synthetic_eth)
    if not bridge:
        print(f"{r['origin_tx']}: NO BRIDGE MATCH"); continue
    benchmark_usdg = synthetic_eth * bridge["rate"]
    difference_usdg = actual_usdg - benchmark_usdg
    difference_pct = difference_usdg / benchmark_usdg * Decimal(100)

    c_blocks = canonical["nearest_blocks"]; b_blocks = bridge["nearest_blocks"]
    c_ratio = canonical["best_size_ratio"]; b_ratio = bridge["best_size_ratio"]
    if c_blocks <= 3000 and b_blocks <= 3000 and c_ratio <= Decimal(2) and b_ratio <= Decimal(2):
        confidence = "HIGH"
    elif c_blocks <= 18000 and b_blocks <= 18000 and c_ratio <= Decimal(4) and b_ratio <= Decimal(4):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    output.append({
        "origin_tx": r["origin_tx"], "order_id": r["order_id"], "fire_source": r["fire_source"],
        "block": block, "fire_sold": str(fire_sold),
        "actual_relay_deposit_usdg": str(actual_usdg),
        "actual_usdg_per_fire": str(actual_rate),
        "canonical_eth_per_fire": str(canonical["rate"]),
        "synthetic_eth_output": str(synthetic_eth),
        "bridge_usdg_per_eth": str(bridge["rate"]),
        "synthetic_benchmark_usdg": str(benchmark_usdg),
        "actual_minus_benchmark_usdg": str(difference_usdg),
        "actual_vs_benchmark_pct": str(difference_pct),
        "canonical_nearest_block_distance": c_blocks,
        "canonical_best_size_ratio": str(c_ratio),
        "bridge_nearest_block_distance": b_blocks,
        "bridge_best_size_ratio": str(b_ratio),
        "benchmark_confidence": confidence,
        "canonical_match_txs": "|".join(c["tx"] for c in canonical["selected"]),
        "bridge_match_txs": "|".join(c["tx"] for c in bridge["selected"]),
    })
    print(f"[{i:02}/{len(targets)}] FIRE={fire_sold:,.2f}  actual={actual_usdg:,.6f} USDG  "
          f"benchmark={benchmark_usdg:,.6f}  D={difference_pct:+.2f}%  {confidence}")
    time.sleep(0.05)

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(output[0].keys()))
    writer.writeheader(); writer.writerows(output)

print()
print("=" * 80); print("RELAY ORIGIN EXECUTION BENCHMARK"); print("=" * 80)
print(f"Comparable Relay FIRE orders: {len(output)}")
if output:
    diffs = sorted(Decimal(r["actual_vs_benchmark_pct"]) for r in output)
    better = [x for x in diffs if x > 0]; worse = [x for x in diffs if x < 0]
    median = statistics.median([float(x) for x in diffs])
    print(f"Actual better than benchmark: {len(better)}")
    print(f"Actual worse than benchmark:  {len(worse)}")
    print(f"Median actual-vs-benchmark:   {median:+.3f}%")
    print(f"Best:                         {max(diffs):+.3f}%")
    print(f"Worst:                        {min(diffs):+.3f}%")
    print()
    print("Confidence:")
    for level in ("HIGH", "MEDIUM", "LOW"):
        n = sum(r["benchmark_confidence"] == level for r in output)
        print(f"  {level:6}: {n}")
print()
print(f"Wrote {OUTPUT}")
