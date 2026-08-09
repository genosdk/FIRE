"""Live canonical-route pricing via the Uniswap v4 Quoter, for direct comparison
against Relay's quoted origin-side proceeds.

The public RPC retains only ~6,200 blocks of state, so historical quotes are
impossible -- but the CURRENT head is queryable. Both Relay's quote and this
canonical quote are therefore taken against the same live state, which makes
this an exact same-state comparison rather than the observational benchmark
used for the historical orders.

Route priced here:  FIRE --(canonical ETH/FIRE 0.30%)--> ETH --(best ETH/USDG v4 pool)--> USDG
"""
import csv, json, subprocess, time
from decimal import Decimal, getcontext
from eth_abi import encode, decode
from eth_utils import keccak

getcontext().prec = 50

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
NATIVE = "0x0000000000000000000000000000000000000000"
FIRE = "0x43eea882b845a8493152ebc55cf30ae9281b02d5"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
CANON = "0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd"
CANON_HOOK = "0xe3fa8fa0d0a3f59c9b08ea0fe36d654a506850cc"

PROBE = "data/analysis/RELAY_QUOTE_PROBE.csv"
OUT = "data/analysis/RELAY_VS_CANONICAL_LIVE.csv"

SEL = keccak(text="quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))")[:4]
ARG = "((address,address,uint24,int24,address),bool,uint128,bytes)"


def rpc(m, p, tries=3):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "40", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"]); time.sleep(0.3 * (a + 1)); continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.3 * (a + 1))
    raise last


def i24(w):
    v = int(w, 16) & 0xFFFFFF
    return v - 0x1000000 if v & 0x800000 else v


def quote(key, zero_for_one, amount_in):
    """key = (c0, c1, fee, tickSpacing, hooks); returns amountOut or None."""
    params = (key, zero_for_one, int(amount_in), b"")
    data = "0x" + (SEL + encode([ARG], [params])).hex()
    try:
        res = rpc("eth_call", [{"to": QUOTER, "data": data, "gas": hex(30_000_000)}, "latest"], tries=2)
        return decode(["uint256", "uint256"], bytes.fromhex(res[2:]))[0]
    except Exception:
        return None


# ---- canonical pool key -------------------------------------------------
lg = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                          "topics": [INIT, CANON]}])[0]
w = [lg["data"][2:][i:i + 64] for i in range(0, len(lg["data"][2:]), 64)]
CANON_KEY = (NATIVE, FIRE, int(w[0], 16) & 0xFFFFFF, i24(w[1]), "0x" + w[2][24:])
print(f"canonical key: fee={CANON_KEY[2]} tickSpacing={CANON_KEY[3]} hooks={CANON_KEY[4]}")
assert CANON_KEY[4].lower() == CANON_HOOK

# ---- enumerate ETH/USDG v4 pools, pick the one giving the best ETH->USDG --
nat_t = "0x" + "00" * 32
usdg_t = "0x" + "00" * 12 + USDG[2:]
pools = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                            "topics": [INIT, None, nat_t, usdg_t]}])
cands = []
for L in pools:
    d = L["data"][2:]
    ws = [d[i:i + 64] for i in range(0, len(d), 64)]
    fee = int(ws[0], 16) & 0xFFFFFF
    if fee > 10000:            # only sane tiers for a benchmark leg
        continue
    cands.append((NATIVE, USDG, fee, i24(ws[1]), "0x" + ws[2][24:]))
print(f"ETH/USDG candidate pools with fee<=1%: {len(cands)}")

PROBE_ETH = 10 ** 16   # 0.01 ETH probe to rank the bridge pools
ranked = []
for k in cands:
    out = quote(k, True, PROBE_ETH)
    if out:
        ranked.append((out, k))
ranked.sort(reverse=True, key=lambda x: x[0])
if not ranked:
    raise SystemExit("no quotable ETH/USDG pool")
BRIDGE = ranked[0][1]
print(f"best bridge pool: fee={BRIDGE[2]} tickSpacing={BRIDGE[3]} hooks={BRIDGE[4]} "
      f"-> {ranked[0][0]/1e6:.6f} USDG per 0.01 ETH")
print(f"runners-up: {[(k[2], f'{o/1e6:.6f}') for o, k in ranked[1:4]]}\n")

# ---- Relay quotes to compare against ------------------------------------
relay = {}
for r in csv.DictReader(open(PROBE)):
    if r["ok"] == "True" and r["variant"] == "default":
        relay[int(r["fire_in"])] = Decimal(r["origin_usdg_expected"])

rows = []
print(f"{'FIRE':>9} {'canonical USDG':>16} {'Relay USDG':>13} {'Relay vs canon':>16}")
for size in sorted(relay):
    amt = int(Decimal(size) * Decimal(10 ** 18))
    eth_out = quote(CANON_KEY, False, amt)          # selling currency1 (FIRE) -> zeroForOne = False
    if not eth_out:
        print(f"{size:>9} canonical quote failed"); continue
    usdg_out = quote(BRIDGE, True, eth_out)         # selling currency0 (ETH) -> zeroForOne = True
    if not usdg_out:
        print(f"{size:>9} bridge quote failed"); continue
    canon = Decimal(usdg_out) / Decimal(10 ** 6)
    rly = relay[size]
    diff = (rly - canon) / canon * 100
    rows.append({"fire_in": size,
                 "canonical_eth_out": str(Decimal(eth_out) / Decimal(10 ** 18)),
                 "canonical_usdg_out": str(canon),
                 "relay_origin_usdg": str(rly),
                 "relay_minus_canonical_usdg": str(rly - canon),
                 "relay_vs_canonical_pct": str(diff),
                 "bridge_fee": BRIDGE[2], "canonical_fee": CANON_KEY[2]})
    print(f"{size:>9} {canon:>16.6f} {rly:>13.6f} {diff:>15.2f}%")

with open(OUT, "w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    wr.writeheader(); wr.writerows(rows)
print(f"\nWrote {OUT} ({len(rows)} rows)")
worse = sum(1 for r in rows if Decimal(r["relay_vs_canonical_pct"]) < 0)
print(f"Relay worse than canonical: {worse}/{len(rows)}")
