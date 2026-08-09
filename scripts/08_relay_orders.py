import csv
import json
import os
import subprocess
from eth_abi import decode
from eth_utils import keccak

RPC = "https://rpc.mainnet.chain.robinhood.com"
INPUT = "FIRE_ONE_WAY_TRACE.csv"
OUTPUT = "FIRE_RELAY_ORDERS.csv"

DEPOSITORY = "0x4cd00e387622c35bddb9b4c962c136462338bc31"

ERC20_DEPOSIT_TOPIC = "0x" + keccak(
    text="RelayErc20Deposit(address,address,uint256,bytes32)").hex()
NATIVE_DEPOSIT_TOPIC = "0x" + keccak(
    text="RelayNativeDeposit(address,uint256,bytes32)").hex()

RELAY_API_KEY = os.getenv("RELAY_API_KEY")


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    p = subprocess.run(["curl", "-sS", RPC, "-H", "Content-Type: application/json",
                        "-d", payload], capture_output=True, text=True, check=True)
    data = json.loads(p.stdout)
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def relay_request(order_id):
    if not RELAY_API_KEY:
        return None
    p = subprocess.run(["curl", "-sS",
                        f"https://api.relay.link/requests/v3?orderId={order_id}&limit=5",
                        "-H", f"x-api-key: {RELAY_API_KEY}"],
                       capture_output=True, text=True, check=True)
    data = json.loads(p.stdout)
    requests = data.get("requests", [])
    if not requests:
        return None
    return requests[0]


def addr(value):
    return value.lower() if value else ""


def nested(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


with open(INPUT, newline="", encoding="utf-8") as f:
    source = list(csv.DictReader(f))

targets = [r for r in source
           if r["classification"] == "RFQ_INTENT_SETTLEMENT"
           and addr(r["economic_output_recipient"]) == DEPOSITORY]

print(f"Relay-origin transactions: {len(targets)}")
print(f"ERC20 deposit topic : {ERC20_DEPOSIT_TOPIC}")
print(f"Native deposit topic: {NATIVE_DEPOSIT_TOPIC}\n")

FIELDS = ["origin_tx","fire_source","fire_source_type","fire_sold","v4_output_amount",
          "relay_depositor","deposit_token","deposit_amount_raw","order_id","request_id",
          "relay_user","relay_recipient","relay_sender","status","origin_chain",
          "destination_chain","quoted_origin_input_symbol","quoted_origin_input_amount",
          "actual_origin_input_symbol","actual_origin_input_amount",
          "quoted_destination_output_symbol","quoted_destination_output_amount",
          "actual_destination_output_symbol","actual_destination_output_amount",
          "solver","fill_tx","relay_api_found"]

def blank(**kw):
    row = {k: "" for k in FIELDS}
    row["relay_api_found"] = False
    row.update(kw)
    return row

rows = []

for i, r in enumerate(targets, 1):
    tx_hash = r["tx_hash"]
    receipt = rpc("eth_getTransactionReceipt", [tx_hash])
    deposits = []
    depo_logs = 0
    for log in receipt["logs"]:
        if addr(log["address"]) != DEPOSITORY:
            continue
        depo_logs += 1
        if not log.get("topics"):
            continue
        topic0 = log["topics"][0].lower()
        raw = bytes.fromhex(log["data"][2:])
        if topic0 == ERC20_DEPOSIT_TOPIC.lower():
            depositor, token, amount, order_id = decode(
                ["address", "address", "uint256", "bytes32"], raw)
            deposits.append({"type": "ERC20", "depositor": depositor.lower(),
                             "token": token.lower(), "amount_raw": str(amount),
                             "order_id": "0x" + order_id.hex()})
        elif topic0 == NATIVE_DEPOSIT_TOPIC.lower():
            depositor, amount, order_id = decode(["address", "uint256", "bytes32"], raw)
            deposits.append({"type": "NATIVE", "depositor": depositor.lower(),
                             "token": "0x0000000000000000000000000000000000000000",
                             "amount_raw": str(amount), "order_id": "0x" + order_id.hex()})

    if not deposits:
        print(f"[{i:02}/{len(targets)}] {tx_hash[:12]}… NO DEPOSIT EVENT "
              f"(depository logs present: {depo_logs})")
        rows.append(blank(origin_tx=tx_hash, fire_source=r["economic_input_source"],
                          fire_source_type=r["economic_input_source_type"],
                          fire_sold=r["v4_input_amount"], v4_output_amount=r["v4_output_amount"]))
        continue

    for d in deposits:
        order_id = d["order_id"]
        request = relay_request(order_id)
        vals = {}
        if request:
            vals["request_id"] = str(request.get("id", ""))
            vals["relay_user"] = str(request.get("user", ""))
            vals["relay_recipient"] = str(request.get("recipient", ""))
            vals["relay_sender"] = str(request.get("sender", ""))
            vals["status"] = str(request.get("status", ""))
            quoted = nested(request, "data", "route", "quoted") or {}
            actual = nested(request, "data", "route", "actual") or {}
            q_o = nested(quoted, "origin", "inputCurrency") or {}
            a_o = nested(actual, "origin", "inputCurrency") or {}
            q_d = nested(quoted, "destination", "outputCurrency") or {}
            a_d = nested(actual, "destination", "outputCurrency") or {}
            vals["origin_chain"] = nested(q_o,"currency","chainId") or nested(a_o,"currency","chainId") or ""
            vals["destination_chain"] = nested(q_d,"currency","chainId") or nested(a_d,"currency","chainId") or ""
            vals["quoted_origin_input_symbol"] = nested(q_o,"currency","symbol") or ""
            vals["quoted_origin_input_amount"] = q_o.get("amountFormatted","")
            vals["actual_origin_input_symbol"] = nested(a_o,"currency","symbol") or ""
            vals["actual_origin_input_amount"] = a_o.get("amountFormatted","")
            vals["quoted_destination_output_symbol"] = nested(q_d,"currency","symbol") or ""
            vals["quoted_destination_output_amount"] = q_d.get("amountFormatted","")
            vals["actual_destination_output_symbol"] = nested(a_d,"currency","symbol") or ""
            vals["actual_destination_output_amount"] = a_d.get("amountFormatted","")
            vals["solver"] = nested(request,"protocol","solver","address") or ""
            fills = nested(request,"protocol","settlement","destination","fills") or []
            if fills:
                vals["fill_tx"] = fills[0].get("transactionId","")

        rows.append(blank(origin_tx=tx_hash, fire_source=r["economic_input_source"],
                          fire_source_type=r["economic_input_source_type"],
                          fire_sold=r["v4_input_amount"], v4_output_amount=r["v4_output_amount"],
                          relay_depositor=d["depositor"], deposit_token=d["token"],
                          deposit_amount_raw=d["amount_raw"], order_id=order_id,
                          relay_api_found=bool(request), **vals))
        print(f"[{i:02}/{len(targets)}] {tx_hash[:12]}… order={order_id[:12]}… "
              f"depositor={d['depositor'][:10]}… API={'YES' if request else 'NO'}")

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDS)
    writer.writeheader(); writer.writerows(rows)

print()
print("=" * 80); print("SUMMARY"); print("=" * 80)
print(f"Relay rows: {len(rows)}")
matched = sum(addr(r["relay_depositor"]) == addr(r["fire_source"]) for r in rows if r["relay_depositor"])
withdep = sum(1 for r in rows if r["relay_depositor"])
print(f"Rows with a decoded deposit event: {withdep} / {len(rows)}")
print(f"Depositor == FIRE source: {matched} / {withdep}")
print(f"Relay API records resolved: {sum(bool(r['relay_api_found']) for r in rows)} / {len(rows)}")
destinations = {}
for r in rows:
    d = str(r["destination_chain"])
    if not d: continue
    destinations[d] = destinations.get(d, 0) + 1
print("Destination chains:", destinations)
print(f"Wrote {OUTPUT}")
