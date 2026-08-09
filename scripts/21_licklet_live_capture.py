"""Live case study: the LICKLET deployment burst.

Three extreme-fee pools (87%, 88%, 99%) were created against LICKLET within
~1,200 blocks while this investigation was running. This snapshots the whole
opportunity -- reference market state before deployment, each initialization,
seed capital, first swaps, first pokes -- which historical reconstruction can
never give with the same confidence about what information the deployer had.

Read-only. Nothing is traded.
"""
import json, subprocess, time
from datetime import datetime, timezone
from decimal import Decimal, getcontext
getcontext().prec = 50

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
LICKLET = "0x23192efc86d38f8b9de39af603b73521ec346bcc"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
ML = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
XFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CLUSTER_OP = "0xe49193acd219557a150e35ccc2a66f89c42482e6"
OUT = "data/analysis/LICKLET_LIVE_CAPTURE.json"


def rpc(m, p, tries=4):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "50", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.4 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.4 * (a + 1))
    raise last


def i24(w):
    v = int(w, 16) & 0xFFFFFF
    return v - 0x1000000 if v & 0x800000 else v


def s128(w):
    v = int(w, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >= 1 << 127 else v


def ts(bh):
    return int(rpc("eth_getBlockByNumber", [bh, False])["timestamp"], 16)


def when(b):
    return datetime.fromtimestamp(ts(hex(b)), tz=timezone.utc).isoformat()


def chunked(topics, lo, hi, chunk=40000):
    out, s = [], lo
    while s <= hi:
        e = min(s + chunk - 1, hi)
        for a in range(4):
            try:
                out += rpc("eth_getLogs", [{"address": PM, "fromBlock": hex(s),
                                            "toBlock": hex(e), "topics": topics}], tries=1)
                break
            except Exception:
                time.sleep(0.4 * (a + 1))
        s = e + 1
    return out


latest = int(rpc("eth_blockNumber", []), 16)
tok_t = "0x" + "00" * 12 + LICKLET[2:]

# ---- every pool containing LICKLET, cluster or not ----------------------
pools = []
for topics in ([INIT, None, tok_t, None], [INIT, None, None, tok_t]):
    for L in rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                                  "topics": topics}]):
        d = L["data"][2:]
        w = [d[i:i + 64] for i in range(0, len(d), 64)]
        pools.append({"pool_id": L["topics"][1].lower(),
                      "currency0": "0x" + L["topics"][2][26:],
                      "currency1": "0x" + L["topics"][3][26:],
                      "fee": int(w[0], 16) & 0xFFFFFF, "tick_spacing": i24(w[1]),
                      "hooks": "0x" + w[2][24:],
                      "sqrt_price_init": int(w[3], 16), "tick_init": i24(w[4]),
                      "init_block": int(L["blockNumber"], 16), "init_tx": L["transactionHash"]})
pools.sort(key=lambda p: p["init_block"])
print(f"LICKLET pools on-chain: {len(pools)}\n")

first_block = min(p["init_block"] for p in pools)

# token age: first ever Transfer of LICKLET
tk = rpc("eth_getLogs", [{"address": LICKLET, "fromBlock": "0x0",
                          "toBlock": hex(first_block), "topics": [XFER]}])
tok_first = int(tk[0]["blockNumber"], 16) if tk else None
print(f"LICKLET first transfer: block {tok_first:,} ({when(tok_first)})" if tok_first else "no transfers")

cap = {"token": LICKLET, "captured_at_block": latest, "captured_at": when(latest),
       "token_first_transfer_block": tok_first, "pools": []}

for p in pools:
    is_cluster = p["fee"] >= 100000
    pid = p["pool_id"]
    ml = chunked([ML, pid], p["init_block"], latest)
    sw = chunked([SWAP, pid], p["init_block"], latest)
    seeds, pokes, adds = [], [], []
    for L in ml:
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        dl = int(w[2], 16)
        dl = dl - (1 << 256) if dl >= 1 << 255 else dl
        blk = int(L["blockNumber"], 16)
        tx = rpc("eth_getTransactionByHash", [L["transactionHash"]])
        rec = {"block": blk, "tx": L["transactionHash"], "tx_from": tx["from"].lower(),
               "liquidity_delta": dl, "salt_token_id": int(w[3], 16)}
        # token amounts moved by the LP in this tx
        rc = rpc("eth_getTransactionReceipt", [L["transactionHash"]])
        amt = {}
        for lg in rc["logs"]:
            if not lg["topics"] or lg["topics"][0].lower() != XFER or len(lg["topics"]) < 3:
                continue
            a = lg["address"].lower()
            frm = ("0x" + lg["topics"][1][26:]).lower(); to = ("0x" + lg["topics"][2][26:]).lower()
            if PM not in (frm, to):
                continue
            v = int(lg["data"], 16)
            sign = 1 if to == PM else -1
            amt[a] = amt.get(a, 0) + sign * v
        rec["token_flows"] = {k: str(v) for k, v in amt.items()}
        (seeds if dl > 0 and not seeds else pokes if dl == 0 else adds).append(rec)
    swaps = []
    for L in sw[:20]:
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        blk = int(L["blockNumber"], 16)
        tx = rpc("eth_getTransactionByHash", [L["transactionHash"]])
        swaps.append({"block": blk, "tx": L["transactionHash"],
                      "swap_sender": ("0x" + L["topics"][2][26:]).lower(),
                      "tx_from": tx["from"].lower(),
                      "amount0": s128(w[0]), "amount1": s128(w[1]),
                      "fee_charged": int(w[5], 16) & 0xFFFFFF,
                      "blocks_after_init": blk - p["init_block"]})
    entry = {**p, "init_time": when(p["init_block"]),
             "blocks_after_first_pool": p["init_block"] - first_block,
             "is_extreme_fee": is_cluster,
             "tick_spacing_equals_fee_over_100": p["tick_spacing"] == p["fee"] // 100,
             "n_modify_liquidity": len(ml), "n_swaps": len(sw),
             "seed": seeds[0] if seeds else None,
             "n_additional_adds": len(adds), "n_pokes": len(pokes),
             "first_swaps": swaps}
    cap["pools"].append(entry)
    fp = f"{p['fee']/10000:.2f}%"
    print(f"pool {pid[:18]}…  fee={fp:>8}  ts={p['tick_spacing']:>6}  "
          f"init block {p['init_block']:,} (+{entry['blocks_after_first_pool']})  "
          f"MLs={len(ml)} swaps={len(sw)} pokes={len(pokes)} adds={len(adds)}")

json.dump(cap, open(OUT, "w"), indent=1)
print(f"\nWrote {OUT}")

print("\n" + "=" * 74)
print("LICKLET DEPLOYMENT SEQUENCE")
print("=" * 74)
for e in cap["pools"]:
    fp = f"{e['fee']/10000:.2f}%"
    s = e["seed"]
    print(f"\n{fp:>8} pool  {e['pool_id'][:20]}…")
    print(f"   initialized  block {e['init_block']:,}  {e['init_time'][:19]}  (+{e['blocks_after_first_pool']} blocks)")
    print(f"   tickSpacing  {e['tick_spacing']}  (fee/100 = {e['fee']//100})  "
          f"{'MATCH' if e['tick_spacing_equals_fee_over_100'] else 'differs'}")
    print(f"   init tick    {e['tick_init']}")
    if s:
        flows = {k[:10]: (int(v) / 1e18 if k.lower() != USDG else int(v) / 1e6) for k, v in s["token_flows"].items()}
        print(f"   seeded by    {s['tx_from']}  block {s['block']:,} (+{s['block']-e['init_block']})")
        print(f"   seed flows   {flows}")
    print(f"   swaps={e['n_swaps']}  extra adds={e['n_additional_adds']}  pokes={e['n_pokes']}")
    for sw in e["first_swaps"][:3]:
        print(f"     swap +{sw['blocks_after_init']:>5} blocks  sender={sw['swap_sender'][:14]}…  "
              f"fee_charged={sw['fee_charged']}")
