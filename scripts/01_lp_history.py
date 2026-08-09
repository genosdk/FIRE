import csv
import json
import subprocess
import time
from datetime import datetime, timezone

RPC = "https://rpc.mainnet.chain.robinhood.com"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

MODIFY_LIQUIDITY_TOPIC = (
    "0xf208f4912782fd25c7f114ca3723a2d5"
    "dd6f3bcc3ac8db5af63baa85f711d5ec"
)

POOLS = {
    "FIRE_USDG_50pct": {
        "pool_id":
            "0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43",
        "creator":
            "0xe49193acd219557a150e35ccc2a66f89c42482e6",
        "creation_tx":
            "0x88662c6e3b98c33709dece6fca5186867fdb07b77eebfc79365e536673f80c35",
    },

    "FIRE_USDG_40pct": {
        "pool_id":
            "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636",
        "creator":
            "0xc6312da2663570a2aee12b27a0c6da4683339acd",
        "creation_tx":
            "0x8689ba10c676b7509644c5d60e3c3dbaa25e2a5131456af4667738f6bfb29268",
    },
}


def rpc(method, params):
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    })

    result = subprocess.run(
        ["curl", "-s", RPC, "-H", "Content-Type: application/json", "-d", payload],
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(result.stdout)

    if "error" in data:
        raise RuntimeError(f"{method}: {json.dumps(data['error'], indent=2)}")

    return data["result"]


def signed(value, bits=256):
    max_value = 1 << bits
    if value >= (1 << (bits - 1)):
        value -= max_value
    return value


def decode_int24(word):
    value = int(word, 16) & 0xFFFFFF
    if value & 0x800000:
        value -= 0x1000000
    return value


block_cache = {}
tx_cache = {}


def get_block(block_hex):
    if block_hex not in block_cache:
        block = rpc("eth_getBlockByNumber", [block_hex, False])
        block_cache[block_hex] = block
        time.sleep(0.03)
    return block_cache[block_hex]


def get_tx(tx_hash):
    if tx_hash not in tx_cache:
        tx = rpc("eth_getTransactionByHash", [tx_hash])
        tx_cache[tx_hash] = tx
        time.sleep(0.03)
    return tx_cache[tx_hash]


def decode_modify_liquidity(log, pool_name, known_creator):
    topics = log["topics"]
    pool_id = topics[1]
    sender = "0x" + topics[2][-40:]

    raw = log["data"][2:]
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]

    if len(words) < 4:
        raise ValueError(f"Unexpected ModifyLiquidity data: {log['data']}")

    tick_lower = decode_int24(words[0])
    tick_upper = decode_int24(words[1])

    liquidity_raw = int(words[2], 16)
    liquidity_delta = signed(liquidity_raw, 256)

    salt = "0x" + words[3]
    token_id = int(words[3], 16)

    if liquidity_delta > 0:
        action = "ADD"
    elif liquidity_delta < 0:
        action = "REMOVE"
    else:
        action = "ZERO"

    block_hex = log["blockNumber"]
    block_number = int(block_hex, 16)

    block = get_block(block_hex)
    timestamp = int(block["timestamp"], 16)
    timestamp_utc = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    tx_hash = log["transactionHash"]
    tx = get_tx(tx_hash)
    tx_from = tx["from"].lower() if tx else ""

    creator_match = (tx_from == known_creator.lower())

    return {
        "pool": pool_name,
        "block": block_number,
        "timestamp_utc": timestamp_utc,
        "transaction_hash": tx_hash,
        "transaction_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_from": tx_from,
        "modify_sender": sender.lower(),
        "pool_id": pool_id.lower(),
        "action": action,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "liquidity_delta": liquidity_delta,
        "abs_liquidity_delta": abs(liquidity_delta),
        "salt": salt.lower(),
        "possible_token_id": token_id,
        "tx_from_is_original_creator": creator_match,
    }


def get_pool_history(pool_name, info):
    print()
    print("=" * 80)
    print(pool_name)
    print(info["pool_id"])
    print("=" * 80)

    logs = rpc("eth_getLogs", [{
        "address": POOL_MANAGER,
        "fromBlock": "0x0",
        "toBlock": "latest",
        "topics": [MODIFY_LIQUIDITY_TOPIC, info["pool_id"]],
    }])

    print(f"ModifyLiquidity events found: {len(logs)}")

    decoded = []

    for i, log in enumerate(logs, 1):
        row = decode_modify_liquidity(log, pool_name, info["creator"])
        decoded.append(row)
        print(
            f"[{i:03}/{len(logs):03}] "
            f"{row['timestamp_utc']}  "
            f"{row['action']:6}  "
            f"dL={row['liquidity_delta']}  "
            f"NFT={row['possible_token_id']}  "
            f"from={row['tx_from']}  "
            f"tx={row['transaction_hash']}"
        )

    decoded.sort(key=lambda x: (x["block"], x["transaction_index"], x["log_index"]))
    return decoded


def write_csv(filename, rows):
    if not rows:
        print(f"No rows for {filename}")
        return

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {filename}")


all_rows = []

for pool_name, info in POOLS.items():
    rows = get_pool_history(pool_name, info)
    filename = f"{pool_name}_modify_liquidity.csv"
    write_csv(filename, rows)
    all_rows.extend(rows)

all_rows.sort(key=lambda x: (x["block"], x["transaction_index"], x["log_index"]))
write_csv("FIRE_USDG_ALL_modify_liquidity.csv", all_rows)

print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)

for pool_name in POOLS:
    rows = [x for x in all_rows if x["pool"] == pool_name]
    adds = [x for x in rows if x["liquidity_delta"] > 0]
    removes = [x for x in rows if x["liquidity_delta"] < 0]
    unique_nfts = sorted(set(x["possible_token_id"] for x in rows))
    unique_tx_from = sorted(set(x["tx_from"] for x in rows))

    print()
    print(pool_name)
    print(f"  Events:              {len(rows)}")
    print(f"  Adds:                {len(adds)}")
    print(f"  Removes:             {len(removes)}")
    print(f"  Unique NFT salts:    {len(unique_nfts)}")
    print(f"  Unique tx.from:      {len(unique_tx_from)}")
    print(f"  NFT IDs:             {unique_nfts}")
    print("  tx.from addresses:")
    for address in unique_tx_from:
        print(f"    {address}")

print()
print("Done.")
