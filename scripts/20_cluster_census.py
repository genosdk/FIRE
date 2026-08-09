"""Pass 1 of the cluster census: the complete v4 LP footprint of the 12 operators.

Every Uniswap v4 position is an ERC-721 minted by the PositionManager, so each
operator's entire footprint is one indexed log query -- no need to scan
ModifyLiquidity chain-wide. Position -> PoolKey comes from
getPoolAndPositionInfo, batched over JSON-RPC (the node supports batching, which
turns ~9,500 sequential calls into a few dozen requests).

PoolId is derived locally as keccak256(abi.encode(PoolKey)), verified against the
two known FIRE pool ids before use.
"""
import csv, json, os, subprocess, time
from collections import defaultdict, Counter
from decimal import Decimal
from eth_abi import encode
from eth_utils import keccak

RPC = "https://rpc.mainnet.chain.robinhood.com"
POSM = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
XFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PI = "0x7ba03aad"      # getPoolAndPositionInfo(uint256)
PL = "0x1efeed33"      # getPositionLiquidity(uint256)

PRIMARY = ["0x7777de7a669c6e76e059fae0e15a0ea041fd752c", "0x2179ecfa0619c125ca0fd28599cede9c239fbf2b",
           "0xe49193acd219557a150e35ccc2a66f89c42482e6", "0x1aa511c41636dc4e1dafe5092157eb606da01d03",
           "0x490f1d47c41ba36f103a8db40b3c4db532b41899", "0x9dfdbeb37f2fd8744db65f6752c66beba351bdee"]
SECONDARY = ["0x1983824aa3cf023dfd233115e801520b9d49a258", "0x271070932c8111fb474a3ef55b9a758fd6b115cc",
             "0x677c32e3d7993e04ffbd910566ee9dff59448070", "0xa1e4bca023d741bd8d8609875ceef2abd8d755a3",
             "0xc6312da2663570a2aee12b27a0c6da4683339acd", "0xd0010d99334e89b18f42c1517ad69675113878da"]
COHORT = {**{a: "PRIMARY" for a in PRIMARY}, **{a: "SECONDARY" for a in SECONDARY}}

FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
KNOWN_POOLS = {"0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43": "FIRE_USDG_50pct",
               "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636": "FIRE_USDG_40pct"}
CACHE = "data/analysis/_census_positions.json"
OUT_POS = "data/analysis/CENSUS_POSITIONS.csv"
OUT_POOL = "data/analysis/CENSUS_POOLS.csv"


def rpc(m, p, tries=4):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "60", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.4 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.4 * (a + 1))
    raise last


def batch(calls, tries=4):
    """calls = [(id, to, data)] -> {id: result}"""
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]} for i, to, data in calls]
    for a in range(tries):
        try:
            out = subprocess.run(["curl", "-sS", "--max-time", "90", RPC,
                                  "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
                                 capture_output=True, text=True, check=True).stdout
            r = json.loads(out)
            if isinstance(r, list):
                return {x["id"]: x.get("result") for x in r if "result" in x}
        except Exception:
            pass
        time.sleep(1.0 * (a + 1))
    return {}


def pool_id(c0, c1, fee, ts, hooks):
    return "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"],
                                [c0, c1, fee, ts, hooks])).hex()


assert pool_id(FIRE, "0x5fc5360d0400a0fd4f2af552add042d716f1d168", 500000, 10000,
               "0x0000000000000000000000000000000000000000") in KNOWN_POOLS, "poolId derivation mismatch"
print("poolId derivation verified against known FIRE pools\n")

# ---- 1. every position NFT each operator has received --------------------
if os.path.exists(CACHE):
    recs = json.load(open(CACHE))
    print(f"loaded {len(recs)} cached positions")
else:
    recs = {}
    for a in PRIMARY + SECONDARY:
        t = "0x" + "00" * 12 + a[2:]
        lg = rpc("eth_getLogs", [{"address": POSM, "fromBlock": "0x0", "toBlock": "latest",
                                  "topics": [XFER, None, t]}])
        for L in lg:
            tid = int(L["topics"][3], 16)
            frm = ("0x" + L["topics"][1][26:]).lower()
            k = str(tid)
            if k not in recs or int(L["blockNumber"], 16) < recs[k]["block"]:
                recs[k] = {"token_id": tid, "owner": a, "cohort": COHORT[a],
                           "block": int(L["blockNumber"], 16), "tx": L["transactionHash"],
                           "minted": frm == "0x" + "00" * 20}
        print(f"  {a} {COHORT[a]:9} {len(lg)} transfers in")
    json.dump(recs, open(CACHE, "w"))
    print(f"\nunique positions: {len(recs)}")

ids = sorted(int(k) for k in recs)
print(f"resolving PoolKey + liquidity for {len(ids)} positions...")

# ---- 2. batched pool key + current liquidity -----------------------------
info, liqv = {}, {}
B = 200
for i in range(0, len(ids), B):
    chunk = ids[i:i + B]
    r1 = batch([(t, POSM, PI + f"{t:064x}") for t in chunk])
    r2 = batch([(t, POSM, PL + f"{t:064x}") for t in chunk])
    info.update(r1); liqv.update(r2)
    print(f"  {min(i+B, len(ids))}/{len(ids)}")

# ---- 3. token metadata ---------------------------------------------------
def parse_key(res):
    if not res or len(res) < 2 + 64 * 5:
        return None
    w = [res[2:][j:j + 64] for j in range(0, len(res[2:]), 64)]
    fee = int(w[2], 16) & 0xFFFFFF
    ts = int(w[3], 16)
    if ts >= 1 << 23:
        ts -= 1 << 24
    return ("0x" + w[0][24:], "0x" + w[1][24:], fee, ts, "0x" + w[4][24:])


keys = {t: parse_key(info.get(t)) for t in ids}
tokens = sorted({k[0] for k in keys.values() if k} | {k[1] for k in keys.values() if k})
print(f"unique tokens touched: {len(tokens)}")
meta = {"0x0000000000000000000000000000000000000000": ("ETH", 18)}
todo = [t for t in tokens if t not in meta]
for i in range(0, len(todo), 100):
    ch = todo[i:i + 100]
    rs = batch([(j, a, "0x95d89b41") for j, a in enumerate(ch)])
    rd = batch([(j, a, "0x313ce567") for j, a in enumerate(ch)])
    for j, a in enumerate(ch):
        sym = a[:10]
        try:
            raw = bytes.fromhex((rs.get(j) or "0x")[2:])
            ln = int.from_bytes(raw[32:64], "big")
            sym = raw[64:64 + ln].decode(errors="replace") or sym
        except Exception:
            pass
        try:
            dec = int(rd.get(j) or "0x12", 16)
        except Exception:
            dec = 18
        meta[a] = (sym, dec)

# ---- 4. rows -------------------------------------------------------------
rows = []
for t in ids:
    k = keys[t]
    if not k:
        continue
    c0, c1, fee, ts, hooks = k
    pid = pool_id(c0, c1, fee, ts, hooks)
    try:
        L = int(liqv.get(t) or "0x0", 16)
    except Exception:
        L = 0
    r = recs[str(t)]
    rows.append({"token_id": t, "operator": r["owner"], "cohort": r["cohort"],
                 "pool_id": pid, "pool_label": KNOWN_POOLS.get(pid, ""),
                 "token0": c0, "token0_symbol": meta.get(c0, ("?", 18))[0],
                 "token1": c1, "token1_symbol": meta.get(c1, ("?", 18))[0],
                 "fee": fee, "fee_pct": fee / 10000 if fee != 0x800000 else "dynamic",
                 "tick_spacing": ts, "hooks": hooks,
                 "mint_block": r["block"], "mint_tx": r["tx"], "minted_directly": r["minted"],
                 "current_liquidity": L, "active": L > 0})

with open(OUT_POS, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

pools = defaultdict(lambda: {"ops": set(), "cohorts": set(), "n": 0, "first": 10**18, "last": 0,
                             "active": 0, "fee": 0, "t0": "", "t1": "", "hooks": ""})
for r in rows:
    p = pools[r["pool_id"]]
    p["ops"].add(r["operator"]); p["cohorts"].add(r["cohort"]); p["n"] += 1
    p["first"] = min(p["first"], r["mint_block"]); p["last"] = max(p["last"], r["mint_block"])
    p["active"] += bool(r["active"])
    p["fee"] = r["fee"]; p["t0"] = r["token0_symbol"]; p["t1"] = r["token1_symbol"]; p["hooks"] = r["hooks"]
prows = []
for pid, p in sorted(pools.items(), key=lambda kv: -kv[1]["n"]):
    prows.append({"pool_id": pid, "pair": f"{p['t0']}/{p['t1']}", "fee": p["fee"],
                  "fee_pct": p["fee"] / 10000 if p["fee"] != 0x800000 else "dynamic",
                  "hooks": p["hooks"], "cluster_positions": p["n"],
                  "cluster_operators": len(p["ops"]), "cohorts": "|".join(sorted(p["cohorts"])),
                  "operators": "|".join(sorted(p["ops"])),
                  "first_mint_block": p["first"], "last_mint_block": p["last"],
                  "active_positions": p["active"],
                  "pool_label": KNOWN_POOLS.get(pid, "")})
with open(OUT_POOL, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(prows[0].keys())); w.writeheader(); w.writerows(prows)

# ---- 5. summary ----------------------------------------------------------
print("\n" + "=" * 78)
print("CLUSTER FOOTPRINT")
print("=" * 78)
toks = {r["token0_symbol"] for r in rows} | {r["token1_symbol"] for r in rows}
print(f"unique positions:        {len(rows)}")
print(f"unique pools:            {len(prows)}")
print(f"unique tokens:           {len(toks)}")
print(f"active positions:        {sum(1 for r in rows if r['active'])}")
prim = {r["pool_id"] for r in rows if r["cohort"] == "PRIMARY"}
sec = {r["pool_id"] for r in rows if r["cohort"] == "SECONDARY"}
print(f"pools touched by PRIMARY:        {len(prim)}")
print(f"pools touched by SECONDARY only: {len(sec - prim)}")
print(f"pools touched by both cohorts:   {len(prim & sec)}")

print("\nfee tier distribution (by pool):")
buckets = [(0, 100, "<=0.01%"), (100, 500, "0.01-0.05%"), (500, 3000, "0.05-0.30%"),
           (3000, 10000, "0.30-1%"), (10000, 50000, "1-5%"), (50000, 100000, "5-10%"),
           (100000, 400000, "10-40%"), (400000, 1000001, "40%+")]
fees = [p["fee"] for p in prows if p["fee"] != 0x800000]
for lo, hi, lab in buckets:
    n = sum(1 for f in fees if lo <= f < hi)
    if n: print(f"  {lab:12} n={n}")
dyn = sum(1 for p in prows if p["fee"] == 0x800000)
if dyn: print(f"  {'dynamic':12} n={dyn}")
print(f"  extreme (>=10%): {sum(1 for f in fees if f >= 100000)} of {len(fees)} "
      f"({sum(1 for f in fees if f >= 100000)/len(fees)*100:.1f}%)")

print("\nshared-operator rate:")
for k in (2, 3, 4, 5):
    print(f"  pools with >={k} cluster operators: {sum(1 for p in prows if p['cluster_operators'] >= k)}")

print("\ntop 20 pools by cluster positions:")
print(f"  {'pair':22} {'fee':>9} {'pos':>4} {'ops':>4}  pool")
for p in prows[:20]:
    fp = f"{p['fee_pct']}%" if p["fee_pct"] != "dynamic" else "dynamic"
    print(f"  {p['pair'][:22]:22} {fp:>9} {p['cluster_positions']:>4} {p['cluster_operators']:>4}  {p['pool_id'][:18]}…")
