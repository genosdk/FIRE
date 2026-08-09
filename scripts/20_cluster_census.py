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


BATCH_MAX = 50          # the node returns 429 on batches of >=100


def batch(calls, tries=5):
    """calls = [(id, to, data)] -> {id: result}. Raises if a batch never lands,
    rather than returning {} -- a silent empty result previously produced a
    complete run in which every PoolKey resolved to None."""
    if len(calls) > BATCH_MAX:
        out = {}
        for i in range(0, len(calls), BATCH_MAX):
            out.update(batch(calls[i:i + BATCH_MAX], tries))
        return out
    payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                "params": [{"to": to, "data": data}, "latest"]} for i, to, data in calls]
    last = ""
    for a in range(tries):
        try:
            out = subprocess.run(["curl", "-sS", "--max-time", "90", RPC,
                                  "-H", "Content-Type: application/json", "-d", json.dumps(payload)],
                                 capture_output=True, text=True, check=True).stdout
            r = json.loads(out)
            if isinstance(r, list) and r:
                got = {x["id"]: x.get("result") for x in r if "result" in x}
                if got:
                    return got
            last = str(out)[:160]
        except Exception as e:
            last = str(e)[:160]
        time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"batch of {len(calls)} failed after {tries} attempts: {last}")


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
            ev = {"block": int(L["blockNumber"], 16), "from": frm, "to": a,
                  "tx": L["transactionHash"]}
            if k not in recs:
                recs[k] = {"token_id": tid, "events": []}
            recs[k]["events"].append(ev)
        print(f"  {a} {COHORT[a]:9} {len(lg)} transfers in")
    for k in recs:
        recs[k]["events"].sort(key=lambda e: e["block"])
    json.dump(recs, open(CACHE, "w"))
    print(f"\nunique positions: {len(recs)}")

ids = sorted(int(k) for k in recs)
print(f"resolving PoolKey + liquidity for {len(ids)} positions...")

# ---- 2. batched pool key + current liquidity -----------------------------
RCACHE = "data/analysis/_census_resolved.json"
if os.path.exists(RCACHE):
    _rc = json.load(open(RCACHE))
    info = {int(k): v for k, v in _rc["info"].items()}
    liqv = {int(k): v for k, v in _rc["liq"].items()}
    print(f"loaded cached resolution for {len(info)} positions")
else:
    info, liqv = {}, {}
B = 50
todo_ids = [t for t in ids if t not in info]
for i in range(0, len(todo_ids), B):
    chunk = todo_ids[i:i + B]
    r1 = batch([(t, POSM, PI + f"{t:064x}") for t in chunk])
    r2 = batch([(t, POSM, PL + f"{t:064x}") for t in chunk])
    info.update(r1); liqv.update(r2)
    time.sleep(0.2)
    if (i // B) % 10 == 0 or i + B >= len(todo_ids):
        print(f"  {min(i+B, len(todo_ids))}/{len(todo_ids)}")
if todo_ids:
    json.dump({"info": {str(k): v for k, v in info.items()},
               "liq": {str(k): v for k, v in liqv.items()}}, open(RCACHE, "w"))

# current owner, batched
OWNER = "0x6352211e"
owners = {}
for i in range(0, len(ids), B):
    ch = ids[i:i + B]
    owners.update(batch([(t, POSM, OWNER + f"{t:064x}") for t in ch]))

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
resolved = sum(1 for v in keys.values() if v)
print(f"resolved PoolKeys: {resolved}/{len(ids)}")
if resolved < len(ids) * 0.99:
    raise SystemExit(f"ABORT: only {resolved}/{len(ids)} PoolKeys resolved -- "
                     "refusing to report a footprint built on failed calls")
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG_A = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE_A = "0x0000000000000000000000000000000000000000"
ETHY = {WETH, NATIVE_A}


def topology(c0, c1, s0, s1):
    a, b = c0.lower(), c1.lower()
    stable = lambda x, sym: x == USDG_A or sym.upper() in ("USDC", "USDT", "DAI", "USDG")
    st0, st1 = stable(a, s0), stable(b, s1)
    e0, e1 = a in ETHY, b in ETHY
    if st0 and st1: return "stable/stable"
    if (e0 and st1) or (e1 and st0): return "ETH/stable"
    if e0 and e1: return "ETH/WETH"
    if st0 or st1: return "token/stable"
    if e0 or e1: return "token/ETH"
    return "token/token"
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
    evs = r["events"]
    mint_ev = next((e for e in evs if e["from"] == "0x" + "00" * 20), None)
    mint_recipient = mint_ev["to"] if mint_ev else ""
    first_owner = evs[0]["to"]
    owners_ever = sorted({e["to"] for e in evs})
    cur = owners.get(t)
    cur = ("0x" + cur[-40:]).lower() if cur and len(cur) >= 42 else ""
    rows.append({"token_id": t, "operator": first_owner, "cohort": COHORT[first_owner],
                 "mint_recipient": mint_recipient, "first_cluster_owner": first_owner,
                 "current_owner": cur, "current_owner_in_cluster": cur in COHORT,
                 "cluster_owners_ever": "|".join(owners_ever),
                 "n_cluster_owners_ever": len(owners_ever),
                 "pool_id": pid, "pool_label": KNOWN_POOLS.get(pid, ""),
                 "token0": c0, "token0_symbol": meta.get(c0, ("?", 18))[0],
                 "token1": c1, "token1_symbol": meta.get(c1, ("?", 18))[0],
                 "fee": fee, "fee_pct": fee / 10000 if fee != 0x800000 else "dynamic",
                 "tick_spacing": ts, "hooks": hooks,
                 "hooked": hooks.lower() != "0x" + "00" * 20,
                 "topology": topology(c0, c1, meta.get(c0, ("?", 18))[0], meta.get(c1, ("?", 18))[0]),
                 "mint_block": evs[0]["block"], "mint_tx": evs[0]["tx"],
                 "minted_directly": bool(mint_ev),
                 "current_liquidity": L, "active": L > 0})

with open(OUT_POS, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

pools = defaultdict(lambda: {"ops": set(), "cohorts": set(), "n": 0, "first": 10**18, "last": 0,
                             "active": 0, "fee": 0, "t0": "", "t1": "", "hooks": "",
                             "topology": "", "hooked": False})
for r in rows:
    p = pools[r["pool_id"]]
    p["ops"].add(r["operator"]); p["cohorts"].add(r["cohort"]); p["n"] += 1
    p["first"] = min(p["first"], r["mint_block"]); p["last"] = max(p["last"], r["mint_block"])
    p["active"] += bool(r["active"])
    p["fee"] = r["fee"]; p["t0"] = r["token0_symbol"]; p["t1"] = r["token1_symbol"]; p["hooks"] = r["hooks"]
    p["topology"] = r["topology"]; p["hooked"] = r["hooked"]
prows = []
for pid, p in sorted(pools.items(), key=lambda kv: -kv[1]["n"]):
    prows.append({"pool_id": pid, "pair": f"{p['t0']}/{p['t1']}", "fee": p["fee"],
                  "fee_pct": p["fee"] / 10000 if p["fee"] != 0x800000 else "dynamic",
                  "hooks": p["hooks"], "cluster_positions": p["n"],
                  "cluster_operators": len(p["ops"]), "cohorts": "|".join(sorted(p["cohorts"])),
                  "operators": "|".join(sorted(p["ops"])),
                  "first_mint_block": p["first"], "last_mint_block": p["last"],
                  "active_positions": p["active"], "topology": p["topology"], "hooked": p["hooked"],
                  "pool_label": KNOWN_POOLS.get(pid, "")})
with open(OUT_POOL, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(prows[0].keys())); w.writeheader(); w.writerows(prows)

# ---- 5. summary ----------------------------------------------------------
BASE = {"ETH", "WETH", "USDG", "USDC", "USDT", "DAI"}
pairs = {tuple(sorted((r["token0_symbol"], r["token1_symbol"]))) for r in rows}
nonbase = {sym for r in rows for sym in (r["token0_symbol"], r["token1_symbol"])} - BASE

print("\n" + "=" * 78)
print("CLUSTER FOOTPRINT")
print("=" * 78)
print(f"distinct position NFTs:   {len(rows)}")
print(f"distinct pools:           {len(prows)}")
print(f"distinct token pairs:     {len(pairs)}")
print(f"distinct non-base tokens: {len(nonbase)}")
print(f"positions per pool:       {len(rows)/len(prows):.1f}")
print(f"active positions:         {sum(1 for r in rows if r['active'])}")

xfer = [r for r in rows if r["n_cluster_owners_ever"] > 1]
notmint = [r for r in rows if not r["minted_directly"]]
outside = [r for r in rows if r["current_owner"] and not r["current_owner_in_cluster"]]
print(f"\npositions held by >1 cluster operator: {len(xfer)}")
print(f"positions not minted to a cluster wallet: {len(notmint)}")
print(f"positions now owned outside the cluster: {len(outside)}")

prim = {r["pool_id"] for r in rows if r["cohort"] == "PRIMARY"}
sec = {r["pool_id"] for r in rows if r["cohort"] == "SECONDARY"}
print(f"\npools touched by PRIMARY:        {len(prim)}")
print(f"pools touched by SECONDARY only: {len(sec - prim)}")
print(f"pools touched by both cohorts:   {len(prim & sec)}")

buckets = [(0, 100, "<=0.01%"), (100, 500, "0.01-0.05%"), (500, 3000, "0.05-0.30%"),
           (3000, 10000, "0.30-1%"), (10000, 50000, "1-5%"), (50000, 100000, "5-10%"),
           (100000, 400000, "10-40%"), (400000, 1000001, "40%+")]
def bucket(f):
    for lo, hi, lab in buckets:
        if lo <= f < hi: return lab
    return "dynamic"

print("\nFEE TIER -- BY UNIQUE POOL")
print(f"  {'fee':14} {'pools':>7} {'% pools':>9}")
bp = Counter(bucket(p["fee"]) if p["fee"] != 0x800000 else "dynamic" for p in prows)
for _, _, lab in buckets + [(0, 0, "dynamic")]:
    if bp.get(lab): print(f"  {lab:14} {bp[lab]:>7} {bp[lab]/len(prows)*100:>8.1f}%")
print("\nFEE TIER -- BY POSITION NFT")
print(f"  {'fee':14} {'positions':>10} {'% positions':>12}")
bn = Counter(bucket(r["fee"]) if r["fee"] != 0x800000 else "dynamic" for r in rows)
for _, _, lab in buckets + [(0, 0, "dynamic")]:
    if bn.get(lab): print(f"  {lab:14} {bn[lab]:>10} {bn[lab]/len(rows)*100:>11.1f}%")
ext_pools = sum(1 for p in prows if p["fee"] != 0x800000 and p["fee"] >= 100000)
ext_pos = sum(1 for r in rows if r["fee"] != 0x800000 and r["fee"] >= 100000)
print(f"\nextreme (>=10%): {ext_pools}/{len(prows)} pools ({ext_pools/len(prows)*100:.1f}%), "
      f"{ext_pos}/{len(rows)} positions ({ext_pos/len(rows)*100:.1f}%)")

print("\nFEE TIER x NUMBER OF CLUSTER OPERATORS IN POOL")
print(f"  {'fee':14} {'1 op':>6} {'2 ops':>6} {'3 ops':>6} {'4+ ops':>7} {'mean ops':>9}")
for _, _, lab in buckets + [(0, 0, "dynamic")]:
    sub = [p for p in prows if (bucket(p["fee"]) if p["fee"] != 0x800000 else "dynamic") == lab]
    if not sub: continue
    c = Counter(min(p["cluster_operators"], 4) for p in sub)
    mean = sum(p["cluster_operators"] for p in sub) / len(sub)
    print(f"  {lab:14} {c.get(1,0):>6} {c.get(2,0):>6} {c.get(3,0):>6} {c.get(4,0):>7} {mean:>9.2f}")

print("\nPAIR TOPOLOGY (by pool)")
for k, v in Counter(p["topology"] for p in prows).most_common():
    print(f"  {k:16} {v:>6} ({v/len(prows)*100:>5.1f}%)")
print("\nHOOKS (by pool)")
hk = Counter("hooked" if p["hooked"] else "hookless" for p in prows)
for k, v in hk.most_common():
    print(f"  {k:16} {v:>6} ({v/len(prows)*100:>5.1f}%)")

print("\nPOSITION MULTIPLICITY")
mult = Counter(p["cluster_positions"] for p in prows)
for k in sorted(mult):
    if k <= 5 or k % 10 == 0:
        print(f"  {k:>4} position(s): {mult[k]:>5} pools")
top = max(prows, key=lambda p: p["cluster_positions"])
print(f"  max: {top['cluster_positions']} positions in {top['pair']} @ {top['fee_pct']}%")

print("\nSHARED-OPERATOR RATE")
for k in (2, 3, 4, 5, 6):
    n = sum(1 for p in prows if p["cluster_operators"] >= k)
    print(f"  pools with >={k} cluster operators: {n} ({n/len(prows)*100:.1f}%)")

print("\nTOP 20 POOLS BY CLUSTER POSITIONS")
print(f"  {'pair':24} {'fee':>9} {'pos':>4} {'ops':>4} {'topology':16}")
for p in prows[:20]:
    fp = f"{p['fee_pct']}%" if p["fee_pct"] != "dynamic" else "dynamic"
    print(f"  {p['pair'][:24]:24} {fp:>9} {p['cluster_positions']:>4} {p['cluster_operators']:>4} {p['topology']:16}")
