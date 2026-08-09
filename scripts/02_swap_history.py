import csv
import json
import subprocess
import time
from datetime import datetime, timezone

RPC = "https://rpc.mainnet.chain.robinhood.com"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

SWAP_TOPIC = (
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7e"
    "e92e57ba5563708daca94dd84ad7112f"
)

FIRE_DECIMALS = 18
USDG_DECIMALS = 6

POOLS = {
    "FIRE_USDG_50pct": {
        "pool_id": "0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43",
        "nominal_fee": 500000,
    },
    "FIRE_USDG_40pct": {
        "pool_id": "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636",
        "nominal_fee": 400000,
    },
}


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    result = subprocess.run(
        ["curl", "-s", RPC, "-H", "Content-Type: application/json", "-d", payload],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    if "error" in data:
        raise RuntimeError(f"{method}: {json.dumps(data['error'], indent=2)}")
    return data["result"]


def signed(value, bits):
    if value >= (1 << (bits - 1)):
        value -= (1 << bits)
    return value


def decode_int24(word):
    value = int(word, 16) & 0xFFFFFF
    if value & 0x800000:
        value -= 0x1000000
    return value


def decode_int128(word):
    value = int(word, 16) & ((1 << 128) - 1)
    return signed(value, 128)


block_cache = {}
tx_cache = {}


def get_block(block_hex):
    if block_hex not in block_cache:
        block_cache[block_hex] = rpc("eth_getBlockByNumber", [block_hex, False])
        time.sleep(0.03)
    return block_cache[block_hex]


def get_tx(tx_hash):
    if tx_hash not in tx_cache:
        tx_cache[tx_hash] = rpc("eth_getTransactionByHash", [tx_hash])
        time.sleep(0.03)
    return tx_cache[tx_hash]


def decode_swap(log, pool_name):
    topics = log["topics"]
    pool_id = topics[1].lower()
    sender = ("0x" + topics[2][-40:]).lower()

    raw = log["data"][2:]
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    if len(words) < 6:
        raise ValueError(f"Unexpected Swap data: {log['data']}")

    amount0_raw = decode_int128(words[0])
    amount1_raw = decode_int128(words[1])
    sqrt_price_x96 = int(words[2], 16)
    liquidity = int(words[3], 16)
    tick = decode_int24(words[4])
    fee = int(words[5], 16) & 0xFFFFFF

    amount0_fire = amount0_raw / (10 ** FIRE_DECIMALS)
    amount1_usdg = amount1_raw / (10 ** USDG_DECIMALS)

    # Uniswap v4 Swap amounts are CALLER-CENTRIC (BalanceDelta of the caller):
    #   amount < 0 -> caller pays that token INTO the pool
    #   amount > 0 -> caller receives that token FROM the pool
    # Verified against physical ERC-20 Transfer logs: for currency0 = FIRE,
    # a negative amount0 coincides with FIRE entering PoolManager, matching to the wei.
    # (An earlier revision of this file assumed the opposite, pool-centric convention.)
    if amount0_raw > 0 and amount1_raw < 0:
        # FIRE leaves the pool, USDG enters -> caller BUYS FIRE
        direction = "USDG_TO_FIRE"
        fire_in = 0; fire_out = amount0_fire
        usdg_in = -amount1_usdg; usdg_out = 0
    elif amount0_raw < 0 and amount1_raw > 0:
        # FIRE enters the pool, USDG leaves -> caller SELLS FIRE
        direction = "FIRE_TO_USDG"
        fire_in = -amount0_fire; fire_out = 0
        usdg_in = 0; usdg_out = amount1_usdg
    else:
        direction = "OTHER"
        fire_in = max(-amount0_fire, 0); fire_out = max(amount0_fire, 0)
        usdg_in = max(-amount1_usdg, 0); usdg_out = max(amount1_usdg, 0)

    if direction == "FIRE_TO_USDG" and fire_in:
        avg_execution_price = usdg_out / fire_in
    elif direction == "USDG_TO_FIRE" and fire_out:
        avg_execution_price = usdg_in / fire_out
    else:
        avg_execution_price = None

    ratio_raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    spot_price_after = ratio_raw * (10 ** (FIRE_DECIMALS - USDG_DECIMALS))

    block_hex = log["blockNumber"]
    block_number = int(block_hex, 16)
    block = get_block(block_hex)
    timestamp = int(block["timestamp"], 16)
    timestamp_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    tx_hash = log["transactionHash"]
    tx = get_tx(tx_hash)
    tx_from = tx["from"].lower() if tx else ""

    return {
        "event_type": "SWAP",
        "pool": pool_name,
        "block": block_number,
        "timestamp_utc": timestamp_utc,
        "transaction_hash": tx_hash,
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_from": tx_from,
        "swap_sender": sender,
        "pool_id": pool_id,
        "direction": direction,
        "amount0_fire_raw": amount0_raw,
        "amount1_usdg_raw": amount1_raw,
        "fire_in": fire_in,
        "fire_out": fire_out,
        "usdg_in": usdg_in,
        "usdg_out": usdg_out,
        "avg_execution_price_usdg_per_fire": avg_execution_price,
        "sqrt_price_x96_after": sqrt_price_x96,
        "spot_price_after_usdg_per_fire": spot_price_after,
        "liquidity_after": liquidity,
        "tick_after": tick,
        "fee_raw": fee,
        "fee_percent": fee / 10000,
    }


def get_pool_swaps(pool_name, info):
    print()
    print("=" * 80)
    print(pool_name)
    print(info["pool_id"])
    print("=" * 80)

    logs = rpc("eth_getLogs", [{
        "address": POOL_MANAGER,
        "fromBlock": "0x0",
        "toBlock": "latest",
        "topics": [SWAP_TOPIC, info["pool_id"]],
    }])

    print(f"Swap events found: {len(logs)}")

    rows = []
    for i, log in enumerate(logs, 1):
        row = decode_swap(log, pool_name)
        rows.append(row)
        print(
            f"[{i:03}/{len(logs):03}] "
            f"{row['timestamp_utc']}  "
            f"{row['direction']:12}  "
            f"FIRE in={row['fire_in']:.6f}  "
            f"FIRE out={row['fire_out']:.6f}  "
            f"USDG in={row['usdg_in']:.6f}  "
            f"USDG out={row['usdg_out']:.6f}  "
            f"fee={row['fee_percent']:.2f}%  "
            f"tx={row['transaction_hash']}"
        )

    rows.sort(key=lambda x: (x["block"], x["transaction_index"], x["log_index"]))
    return rows


def write_csv(filename, rows):
    if not rows:
        print(f"No rows for {filename}")
        return
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {filename}")


all_swaps = []

for pool_name, info in POOLS.items():
    rows = get_pool_swaps(pool_name, info)
    write_csv(f"{pool_name}_swaps.csv", rows)
    all_swaps.extend(rows)

all_swaps.sort(key=lambda x: (x["block"], x["transaction_index"], x["log_index"]))
write_csv("FIRE_USDG_ALL_swaps.csv", all_swaps)

print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)

for pool_name in POOLS:
    rows = [x for x in all_swaps if x["pool"] == pool_name]
    buys = [x for x in rows if x["direction"] == "USDG_TO_FIRE"]
    sells = [x for x in rows if x["direction"] == "FIRE_TO_USDG"]

    total_fire_bought = sum(x["fire_out"] for x in buys)
    total_usdg_spent = sum(x["usdg_in"] for x in buys)
    total_fire_sold = sum(x["fire_in"] for x in sells)
    total_usdg_received = sum(x["usdg_out"] for x in sells)

    print()
    print(pool_name)
    print(f"  Total swaps:           {len(rows)}")
    print(f"  USDG -> FIRE buys:     {len(buys)}")
    print(f"  FIRE -> USDG sells:    {len(sells)}")
    print(f"  FIRE bought:           {total_fire_bought:,.6f}")
    print(f"  USDG spent:            {total_usdg_spent:,.6f}")
    print(f"  FIRE sold:             {total_fire_sold:,.6f}")
    print(f"  USDG received:         {total_usdg_received:,.6f}")

print()
print("Done.")
