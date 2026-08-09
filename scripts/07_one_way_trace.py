import csv
import json
import subprocess
import time
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, getcontext

getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
BLOCKSCOUT = "https://robinhoodchain.blockscout.com"

INPUT_FILE = "FIRE_SUSPECT_route_classification.csv"

OUT_SUMMARY = "FIRE_ONE_WAY_TRACE.csv"
OUT_FLOWS = "FIRE_ONE_WAY_ENDPOINT_FLOWS.csv"
OUT_TRANSFERS = "FIRE_ONE_WAY_TRANSFERS.csv"
OUT_NATIVE = "FIRE_ONE_WAY_NATIVE_TRANSFERS.csv"

MAX_WORKERS = 6
ZERO = "0x0000000000000000000000000000000000000000"

FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
ZEROX_ALLOWANCE_HOLDER = "0x0000000000001ff3684f28c67538d4d072c22734"

TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa" "952ba7f163c4a11628f55a4df523b3ef")
SWAP_TOPIC = ("0x40e9cecb9f5f1f1c5b9c97dec2917b7e" "e92e57ba5563708daca94dd84ad7112f")
INITIALIZE_TOPIC = ("0xdd466e674ea557f56295e2d0218a125e" "a4b4f0f6f3307b95f85e6110838d6438")

KNOWN_INFRA = {
    POOL_MANAGER: "Uniswap V4 PoolManager",
    POSITION_MANAGER: "Uniswap V4 PositionManager",
    UNIVERSAL_ROUTER: "Uniswap Universal Router",
    PERMIT2: "Permit2",
    ZEROX_ALLOWANCE_HOLDER: "0x AllowanceHolder",
    WETH: "WETH contract",
}


def curl_json(args, retries=4):
    last_error = None
    for attempt in range(retries):
        try:
            p = subprocess.run(["curl", "-sS", "--max-time", "35"] + args,
                               capture_output=True, text=True, check=True)
            if not p.stdout.strip():
                raise RuntimeError("empty response")
            return json.loads(p.stdout)
        except Exception as e:
            last_error = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(last_error)


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    data = curl_json([RPC, "-H", "Content-Type: application/json", "-d", payload])
    if "error" in data:
        raise RuntimeError(f"{method}: {json.dumps(data['error'])}")
    return data["result"]


def raw_trace(tx_hash):
    return curl_json([f"{BLOCKSCOUT}/api/v2/transactions/{tx_hash}/raw-trace"])


def signed(value, bits):
    if value >= (1 << (bits - 1)):
        value -= (1 << bits)
    return value


def int128(word):
    value = int(word, 16) & ((1 << 128) - 1)
    return signed(value, 128)


def int24(word):
    value = int(word, 16) & 0xFFFFFF
    if value & 0x800000:
        value -= 0x1000000
    return value


def topic_address(topic):
    return ("0x" + topic[-40:]).lower()


def norm(address):
    return address.lower() if address else ""


token_cache = {
    ZERO: {"symbol": "ETH", "decimals": 18},
    FIRE: {"symbol": "FIRE", "decimals": 18},
    USDG: {"symbol": "USDG", "decimals": 6},
    WETH: {"symbol": "WETH", "decimals": 18},
}
token_lock = threading.Lock()


def decode_string_result(result):
    if not result or result == "0x":
        return None
    b = bytes.fromhex(result[2:])
    try:
        if len(b) >= 64:
            offset = int.from_bytes(b[:32], "big")
            if offset + 32 <= len(b):
                length = int.from_bytes(b[offset:offset + 32], "big")
                start = offset + 32
                raw = b[start:start + length]
                return raw.decode("utf-8", errors="replace")
        return b[:32].rstrip(b"\x00").decode("utf-8", errors="replace")
    except Exception:
        return None


def token_info(address):
    address = norm(address)
    with token_lock:
        if address in token_cache:
            return token_cache[address]
    symbol = address[:10]
    decimals = 18
    try:
        result = rpc("eth_call", [{"to": address, "data": "0x95d89b41"}, "latest"])
        s = decode_string_result(result)
        if s:
            symbol = s
    except Exception:
        pass
    try:
        result = rpc("eth_call", [{"to": address, "data": "0x313ce567"}, "latest"])
        if result and result != "0x":
            decimals = int(result, 16)
    except Exception:
        pass
    info = {"symbol": symbol, "decimals": decimals}
    with token_lock:
        token_cache[address] = info
    return info


def human(token, raw):
    info = token_info(token)
    return Decimal(raw) / Decimal(10 ** info["decimals"])


code_cache = {}
code_lock = threading.Lock()


def address_type(address):
    address = norm(address)
    if not address:
        return ""
    if address in KNOWN_INFRA:
        return "INFRASTRUCTURE"
    with code_lock:
        if address in code_cache:
            return code_cache[address]
    try:
        code = rpc("eth_getCode", [address, "latest"]).lower()
        if code in ("0x", "0x0", ""):
            result = "EOA"
        elif code.startswith("0xef0100"):
            result = "EOA_7702"
        else:
            result = "CONTRACT"
    except Exception:
        result = "UNKNOWN"
    with code_lock:
        code_cache[address] = result
    return result


pool_cache = {}


def get_pool_key(pool_id):
    pool_id = pool_id.lower()
    if pool_id in pool_cache:
        return pool_cache[pool_id]
    logs = rpc("eth_getLogs", [{"address": POOL_MANAGER, "fromBlock": "0x0",
                               "toBlock": "latest", "topics": [INITIALIZE_TOPIC, pool_id]}])
    if len(logs) != 1:
        raise RuntimeError(f"{pool_id}: expected 1 Initialize, got {len(logs)}")
    log = logs[0]
    raw = log["data"][2:]
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    result = {
        "currency0": topic_address(log["topics"][2]),
        "currency1": topic_address(log["topics"][3]),
        "fee": int(words[0], 16) & 0xFFFFFF,
        "tick_spacing": int24(words[1]),
        "hooks": ("0x" + words[2][-40:]).lower(),
    }
    pool_cache[pool_id] = result
    return result


def decode_swap(log):
    pool_id = log["topics"][1].lower()
    key = get_pool_key(pool_id)
    raw = log["data"][2:]
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    return {
        "pool_id": pool_id,
        "currency0": key["currency0"],
        "currency1": key["currency1"],
        "amount0": int128(words[0]),
        "amount1": int128(words[1]),
        "fee": int(words[5], 16) & 0xFFFFFF,
    }


def parse_transfers(receipt):
    transfers = []
    for log in receipt["logs"]:
        if len(log.get("topics", [])) < 3:
            continue
        if log["topics"][0].lower() != TRANSFER_TOPIC:
            continue
        token = log["address"].lower()
        try:
            amount = int(log["data"], 16)
        except Exception:
            continue
        transfers.append({
            "log_index": int(log["logIndex"], 16),
            "token": token,
            "from": topic_address(log["topics"][1]),
            "to": topic_address(log["topics"][2]),
            "amount_raw": amount,
        })
    return transfers


def parse_native_trace(trace, tx):
    flows = []

    def add_flow(frm, to, value, path, source):
        if not frm or not to:
            return
        try:
            value_int = int(value, 16) if isinstance(value, str) else int(value)
        except Exception:
            return
        if value_int <= 0:
            return
        flows.append({"from": norm(frm), "to": norm(to), "value_wei": value_int,
                      "path": str(path), "source": source})

    def parse_flat(items):
        for item in items:
            if item.get("error"):
                continue
            action = item.get("action") or {}
            add_flow(action.get("from"), action.get("to"), action.get("value", "0x0"),
                     item.get("traceAddress", []), "blockscout_flat")

    def walk(node, path="root"):
        if not isinstance(node, dict):
            return
        if not node.get("error"):
            add_flow(node.get("from"), node.get("to"), node.get("value", "0x0"),
                     path, "blockscout_nested")
        for i, child in enumerate(node.get("calls", []) or []):
            walk(child, f"{path}.{i}")

    if isinstance(trace, dict) and "result" in trace:
        trace = trace["result"]
    if isinstance(trace, list):
        if trace and "action" in trace[0]:
            parse_flat(trace)
        else:
            for i, node in enumerate(trace):
                walk(node, str(i))
    elif isinstance(trace, dict):
        walk(trace)

    tx_value = int(tx.get("value", "0x0"), 16)
    if tx_value:
        tx_from = norm(tx["from"])
        tx_to = norm(tx.get("to"))
        already_present = any(f["from"] == tx_from and f["to"] == tx_to and
                              f["value_wei"] == tx_value for f in flows)
        if not already_present:
            add_flow(tx_from, tx_to, tx_value, "root", "tx.value")
    return flows


def fetch_bundle(row):
    tx_hash = row["tx_hash"].lower()
    tx = rpc("eth_getTransactionByHash", [tx_hash])
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    trace = raw_trace(tx_hash)
    return {"source_row": row, "tx": tx, "receipt": receipt, "trace": trace}


with open(INPUT_FILE, newline="", encoding="utf-8") as f:
    source_rows = list(csv.DictReader(f))

print(f"Input transactions: {len(source_rows)}")

bundles = {}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    future_map = {executor.submit(fetch_bundle, row): row["tx_hash"].lower() for row in source_rows}
    done = 0
    for future in as_completed(future_map):
        tx_hash = future_map[future]
        try:
            bundles[tx_hash] = future.result()
        except Exception as e:
            print(f"ERROR {tx_hash}: {e}")
        done += 1
        if done % 20 == 0 or done == len(source_rows):
            print(f"Fetched {done}/{len(source_rows)}")

print(f"Successful bundles: {len(bundles)}")

pool_ids = set()
for bundle in bundles.values():
    receipt = bundle["receipt"]
    for log in receipt["logs"]:
        if norm(log["address"]) != POOL_MANAGER:
            continue
        if not log.get("topics"):
            continue
        if log["topics"][0].lower() != SWAP_TOPIC:
            continue
        pool_ids.add(log["topics"][1].lower())

print(f"Unique V4 pools touched: {len(pool_ids)}")
for i, pool_id in enumerate(sorted(pool_ids), 1):
    try:
        key = get_pool_key(pool_id)
        token_info(key["currency0"])
        token_info(key["currency1"])
    except Exception as e:
        print(f"Pool decode error {pool_id}: {e}")

swap_sender_counts = Counter(norm(row.get("swap_sender")) for row in source_rows if row.get("swap_sender"))
tx_from_counts = Counter(norm(row.get("tx_from")) for row in source_rows if row.get("tx_from"))

analysis = []
for tx_hash, bundle in bundles.items():
    row = bundle["source_row"]
    tx = bundle["tx"]
    receipt = bundle["receipt"]
    v4_net = defaultdict(int)
    v4_swaps = []
    for log in receipt["logs"]:
        if norm(log["address"]) != POOL_MANAGER:
            continue
        if not log.get("topics"):
            continue
        if log["topics"][0].lower() != SWAP_TOPIC:
            continue
        s = decode_swap(log)
        v4_swaps.append(s)
        v4_net[s["currency0"]] += s["amount0"]
        v4_net[s["currency1"]] += s["amount1"]

    nonzero = {token: amount for token, amount in v4_net.items() if amount != 0}
    negatives = [(t, a) for t, a in nonzero.items() if a < 0]
    positives = [(t, a) for t, a in nonzero.items() if a > 0]

    if len(negatives) == 1 and len(positives) == 1:
        economic_shape = "ONE_WAY_ROUTE"
        input_token, input_raw = negatives[0]
        output_token, output_raw = positives[0]
    elif len(negatives) == 0 and len(positives) == 1:
        economic_shape = "CLOSED_CYCLE"
        input_token = ""; input_raw = 0
        output_token, output_raw = positives[0]
    else:
        economic_shape = "MULTI_ASSET_UNRESOLVED"
        input_token = ""; input_raw = 0
        output_token = ""; output_raw = 0

    transfers = parse_transfers(receipt)
    native_flows = parse_native_trace(bundle["trace"], tx)

    analysis.append({
        "tx_hash": tx_hash, "source_row": row, "tx": tx, "receipt": receipt,
        "economic_shape": economic_shape, "input_token": input_token, "input_raw": input_raw,
        "output_token": output_token, "output_raw": output_raw, "v4_net": v4_net,
        "v4_swaps": v4_swaps, "transfers": transfers, "native_flows": native_flows,
    })

shape_counts = Counter(x["economic_shape"] for x in analysis)
print()
print("=" * 80)
print("RE-DERIVED ECONOMIC SHAPES")
print("=" * 80)
for k, v in shape_counts.items():
    print(f"{k:30} {v}")

if shape_counts["ONE_WAY_ROUTE"] != 67:
    print()
    print("WARNING: previous analysis reported 67 ONE_WAY_ROUTE transactions.")
    print(f"This independent pass found {shape_counts['ONE_WAY_ROUTE']}.")
    print("Do not hide this discrepancy. Send me the count if it differs.")

one_way = [x for x in analysis if x["economic_shape"] == "ONE_WAY_ROUTE"]

all_candidate_addresses = set()
for x in one_way:
    token_net = defaultdict(lambda: defaultdict(int))
    for t in x["transfers"]:
        token = t["token"]; frm = t["from"]; to = t["to"]; amount = t["amount_raw"]
        token_net[token][frm] -= amount
        token_net[token][to] += amount
        all_candidate_addresses.add(frm); all_candidate_addresses.add(to)
    native_net = defaultdict(int)
    for f in x["native_flows"]:
        native_net[f["from"]] -= f["value_wei"]
        native_net[f["to"]] += f["value_wei"]
        all_candidate_addresses.add(f["from"]); all_candidate_addresses.add(f["to"])
    x["token_net"] = token_net
    x["native_net"] = native_net

candidate_addresses = [a for a in all_candidate_addresses if a and a != ZERO and a not in KNOWN_INFRA]
print()
print(f"Resolving {len(candidate_addresses)} endpoint address types...")
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(address_type, a): a for a in candidate_addresses}
    for future in as_completed(futures):
        try:
            future.result()
        except Exception:
            pass


def get_net_for_token(x, token):
    if token == ZERO:
        return x["native_net"]
    return x["token_net"].get(token, {})


def is_user_like(address):
    return address_type(address) in ("EOA", "EOA_7702") and address not in KNOWN_INFRA


def best_negative_endpoint(net_map):
    candidates = [(a, v) for a, v in net_map.items() if v < 0 and a != ZERO]
    if not candidates:
        return "", 0
    users = [(a, v) for a, v in candidates if is_user_like(a)]
    if users:
        return min(users, key=lambda kv: kv[1])
    external = [(a, v) for a, v in candidates if a not in KNOWN_INFRA]
    if external:
        return min(external, key=lambda kv: kv[1])
    return min(candidates, key=lambda kv: kv[1])


def best_positive_endpoint(net_map, preferred=""):
    if preferred and net_map.get(preferred, 0) > 0:
        return (preferred, net_map[preferred])
    candidates = [(a, v) for a, v in net_map.items() if v > 0 and a != ZERO]
    if not candidates:
        return "", 0
    users = [(a, v) for a, v in candidates if is_user_like(a)]
    if users:
        return max(users, key=lambda kv: kv[1])
    external = [(a, v) for a, v in candidates if a not in KNOWN_INFRA]
    if external:
        return max(external, key=lambda kv: kv[1])
    return max(candidates, key=lambda kv: kv[1])


summary_rows = []
flow_rows = []
transfer_rows = []
native_rows = []

for x in one_way:
    tx = x["tx"]; row = x["source_row"]
    tx_hash = x["tx_hash"]; tx_from = norm(tx["from"])
    input_token = x["input_token"]; output_token = x["output_token"]
    input_info = token_info(input_token); output_info = token_info(output_token)
    input_net = get_net_for_token(x, input_token)
    output_net = get_net_for_token(x, output_token)
    source, source_delta = best_negative_endpoint(input_net)
    recipient, recipient_delta = best_positive_endpoint(output_net, preferred=source)
    source_type = address_type(source) if source else ""
    recipient_type = address_type(recipient) if recipient else ""

    touched = set()
    for log in x["receipt"]["logs"]:
        touched.add(norm(log["address"]))
    for t in x["transfers"]:
        touched.add(t["from"]); touched.add(t["to"])
    for f in x["native_flows"]:
        touched.add(f["from"]); touched.add(f["to"])

    position_manager_touched = POSITION_MANAGER in touched
    universal_router_touched = (UNIVERSAL_ROUTER in touched or norm(row.get("swap_sender")) == UNIVERSAL_ROUTER)
    permit2_touched = PERMIT2 in touched
    zeroex_touched = ZEROX_ALLOWANCE_HOLDER in touched

    swap_sender = norm(row.get("swap_sender"))
    sender_frequency = swap_sender_counts[swap_sender] if swap_sender else 0
    txfrom_frequency = tx_from_counts[tx_from]

    classification = "UNRESOLVED"; confidence = "LOW"; reason = ""
    fire_disposal = (input_token == FIRE)

    if position_manager_touched:
        classification = "LP_INVENTORY_OPERATION"; confidence = "HIGH"
        reason = "PositionManager touched in same transaction."
    elif fire_disposal and source and is_user_like(source) and source == tx_from:
        classification = "DIRECT_USER_SELL"; confidence = "HIGH"
        reason = "FIRE economic source is the transaction sender."
    elif fire_disposal and source and is_user_like(source) and source != tx_from and recipient == source:
        classification = "AGGREGATOR_USER_SELL"; confidence = "HIGH"
        reason = "EOA/7702 wallet supplies FIRE, different account submits tx, and output returns to FIRE source."
    elif fire_disposal and source and is_user_like(source) and source != tx_from:
        classification = "RFQ_INTENT_SETTLEMENT"; confidence = "MEDIUM"
        reason = "Distinct user-like wallet supplies FIRE while another account submits the transaction."
    elif fire_disposal and source and source_type == "CONTRACT" and (
            sender_frequency >= 3 or txfrom_frequency >= 3 or swap_sender in KNOWN_INFRA):
        classification = "SOLVER_INVENTORY_REBALANCE"; confidence = "MEDIUM"
        reason = "FIRE supplied by contract/infrastructure with repeated router/submitter activity."
    elif fire_disposal and source and source_type == "CONTRACT":
        classification = "OTHER_BOT"; confidence = "LOW"
        reason = "FIRE supplied by contract but solver role is not independently clear."
    elif fire_disposal:
        classification = "UNRESOLVED"; confidence = "LOW"
        reason = "FIRE is the one-way input but no defensible economic source attribution."
    else:
        classification = "OTHER_ONE_WAY_ROUTE"; confidence = "MEDIUM"
        reason = "One-way route does not use FIRE as its economic V4 input."

    v4_input = human(input_token, abs(x["input_raw"]))
    v4_output = human(output_token, x["output_raw"])
    physical_source_amount = human(input_token, abs(source_delta)) if source else Decimal(0)
    physical_recipient_amount = human(output_token, recipient_delta) if recipient else Decimal(0)

    summary_rows.append({
        "tx_hash": tx_hash, "block": row.get("block", ""), "timestamp": row.get("timestamp", ""),
        "route_shape": row.get("v4_hops", ""), "n_v4_hops": row.get("n_v4_hops", ""),
        "tx_from": tx_from, "tx_from_type": address_type(tx_from),
        "tx_from_population_frequency": txfrom_frequency,
        "swap_sender": swap_sender, "swap_sender_population_frequency": sender_frequency,
        "v4_input_token": input_info["symbol"], "v4_input_token_address": input_token,
        "v4_input_amount": str(v4_input),
        "v4_output_token": output_info["symbol"], "v4_output_token_address": output_token,
        "v4_output_amount": str(v4_output),
        "economic_input_source": source, "economic_input_source_type": source_type,
        "physical_input_source_amount": str(physical_source_amount),
        "economic_output_recipient": recipient, "economic_output_recipient_type": recipient_type,
        "physical_output_recipient_amount": str(physical_recipient_amount),
        "input_source_equals_tx_from": source == tx_from,
        "output_returns_to_input_source": (bool(source) and source == recipient),
        "position_manager_touched": position_manager_touched,
        "universal_router_touched": universal_router_touched,
        "permit2_touched": permit2_touched,
        "zeroex_allowanceholder_touched": zeroex_touched,
        "classification": classification, "confidence": confidence,
        "classification_reason": reason,
    })

    for token, address_map in x["token_net"].items():
        info = token_info(token)
        for address, net_raw in address_map.items():
            if net_raw == 0:
                continue
            flow_rows.append({
                "tx_hash": tx_hash, "token": info["symbol"], "token_address": token,
                "address": address, "address_type": address_type(address),
                "known_role": KNOWN_INFRA.get(address, ""),
                "net_raw": net_raw, "net_amount": str(human(token, net_raw)),
            })

    for address, net_raw in x["native_net"].items():
        if net_raw == 0:
            continue
        flow_rows.append({
            "tx_hash": tx_hash, "token": "ETH", "token_address": ZERO,
            "address": address, "address_type": address_type(address),
            "known_role": KNOWN_INFRA.get(address, ""),
            "net_raw": net_raw, "net_amount": str(Decimal(net_raw) / Decimal(10 ** 18)),
        })

    for t in x["transfers"]:
        info = token_info(t["token"])
        transfer_rows.append({
            "tx_hash": tx_hash, "log_index": t["log_index"], "token": info["symbol"],
            "token_address": t["token"], "from": t["from"], "to": t["to"],
            "amount": str(human(t["token"], t["amount_raw"])),
        })

    for f in x["native_flows"]:
        native_rows.append({
            "tx_hash": tx_hash, "from": f["from"], "to": f["to"],
            "amount_eth": str(Decimal(f["value_wei"]) / Decimal(10 ** 18)),
            "trace_path": f["path"], "source": f["source"],
        })


def write_csv(filename, rows):
    if not rows:
        print(f"No rows for {filename}")
        return
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
    print(f"Wrote {filename}: {len(rows)} rows")


write_csv(OUT_SUMMARY, summary_rows)
write_csv(OUT_FLOWS, flow_rows)
write_csv(OUT_TRANSFERS, transfer_rows)
write_csv(OUT_NATIVE, native_rows)

print()
print("=" * 80)
print("ONE-WAY ROUTE ATTRIBUTION")
print("=" * 80)
print(f"One-way routes: {len(summary_rows)}")
fire_rows = [r for r in summary_rows if r["v4_input_token_address"] == FIRE]
print(f"FIRE-input one-way routes: {len(fire_rows)}")
print()
print("Classification:")
for name, count in Counter(r["classification"] for r in summary_rows).most_common():
    print(f"  {count:3}  {name}")
print()
print("Confidence:")
for name, count in Counter(r["confidence"] for r in summary_rows).most_common():
    print(f"  {count:3}  {name}")
print()
print("FIRE source type:")
for name, count in Counter(r["economic_input_source_type"] for r in fire_rows).most_common():
    print(f"  {count:3}  {name or 'NONE'}")
print()
print("FIRE source equals tx.from:")
same_sender = sum(r["input_source_equals_tx_from"] in (True, "True") for r in fire_rows)
print(f"  {same_sender} / {len(fire_rows)}")
print()
print("Output returned to FIRE source:")
same_recipient = sum(r["output_returns_to_input_source"] in (True, "True") for r in fire_rows)
print(f"  {same_recipient} / {len(fire_rows)}")
print()
print("Top economic FIRE sources:")
for address, count in Counter(r["economic_input_source"] for r in fire_rows if r["economic_input_source"]).most_common(15):
    print(f"  {count:3}  {address}  {address_type(address)}")
print()
print("Top economic output recipients:")
for address, count in Counter(r["economic_output_recipient"] for r in fire_rows if r["economic_output_recipient"]).most_common(15):
    print(f"  {count:3}  {address}  {address_type(address)}")
print()
print("Top swap senders:")
for address, count in Counter(r["swap_sender"] for r in summary_rows if r["swap_sender"]).most_common(15):
    label = KNOWN_INFRA.get(address, "")
    print(f"  {count:3}  {address}  {label}")
print()
print("Done.")
