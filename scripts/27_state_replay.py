"""Event-sourced Uniswap v4 state replay + local swap math, with self-validation.

The public RPC retains ~6,200 blocks of state, so historical pool state must be
rebuilt from events: Initialize + ModifyLiquidity + Swap, applied in exact chain
order (blockNumber, transactionIndex, logIndex). Same-block ordering matters here
in a way it did not for deployment rank.

SELF-VALIDATION: every Swap event carries the resulting sqrtPriceX96, the active
liquidity, and both signed amounts. So the engine can be checked against ground
truth on every historical swap:
  * replayed active liquidity vs the liquidity the event reports
  * locally simulated output vs the output the event reports
No fixture needs to be trusted -- the chain states the answer.
"""
import json, subprocess, sys, time
from decimal import Decimal, getcontext
getcontext().prec = 60

RPC = "https://rpc.mainnet.chain.robinhood.com"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
ML = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
Q96 = Decimal(2) ** 96
BASE = Decimal("1.0001")


def rpc(m, p, tries=6):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p})
    last = None
    for a in range(tries):
        try:
            r = json.loads(subprocess.run(["curl", "-sS", "--max-time", "60", RPC,
                                           "-H", "Content-Type: application/json", "-d", b],
                                          capture_output=True, text=True, check=True).stdout)
            if "error" in r:
                last = RuntimeError(r["error"])
                time.sleep(min(20, 3.0 * (a + 1)) if r["error"].get("code") == 429 else 0.4 * (a + 1))
                continue
            return r["result"]
        except Exception as e:
            last = e; time.sleep(0.5 * (a + 1))
    raise last


def logs_adaptive(topics, lo, hi, depth=0):
    try:
        return rpc("eth_getLogs", [{"address": PM, "fromBlock": hex(lo), "toBlock": hex(hi),
                                    "topics": topics}], tries=3)
    except Exception as e:
        if "exceeds limit" not in str(e) or lo >= hi or depth > 12:
            raise
        mid = (lo + hi) // 2
        return logs_adaptive(topics, lo, mid, depth + 1) + logs_adaptive(topics, mid + 1, hi, depth + 1)


def s(v, bits):
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def i24(w): return s(int(w, 16) & 0xFFFFFF, 24)
def i128(w): return s(int(w, 16) & ((1 << 128) - 1), 128)
def i256(w): return s(int(w, 16), 256)
def sqrt_at(tick): return BASE ** (Decimal(tick) / 2)


def key(L):
    return (int(L["blockNumber"], 16), int(L["transactionIndex"], 16), int(L["logIndex"], 16))


def fetch_pool_events(pool_id, init_block, latest):
    ev = []
    for L in logs_adaptive([ML, pool_id], init_block, latest):
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        ev.append({"k": key(L), "t": "ML", "lo": i24(w[0]), "hi": i24(w[1]),
                   "dl": i256(w[2]), "salt": w[3]})
    for L in logs_adaptive([SWAP, pool_id], init_block, latest):
        d = L["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
        ev.append({"k": key(L), "t": "SWAP", "a0": i128(w[0]), "a1": i128(w[1]),
                   "sp": int(w[2], 16), "L": int(w[3], 16), "tick": i24(w[4]),
                   "fee": int(w[5], 16) & 0xFFFFFF, "tx": L["transactionHash"],
                   "sender": ("0x" + L["topics"][2][26:]).lower()})
    ev.sort(key=lambda e: e["k"])
    return ev


class PoolState:
    """Replayed v4 pool state: sqrtPrice, tick, and tick-indexed liquidityNet."""

    def __init__(self, sqrt_price_x96, tick):
        self.sqrtP = Decimal(sqrt_price_x96) / Q96
        self.tick = tick
        self.net = {}          # tick -> liquidityNet
        self.L = 0             # active liquidity

    def apply_ml(self, lo, hi, dl):
        if dl == 0:
            return
        self.net[lo] = self.net.get(lo, 0) + dl
        self.net[hi] = self.net.get(hi, 0) - dl
        if lo <= self.tick < hi:
            self.L += dl

    def recompute_active(self):
        acc = 0
        for t in sorted(self.net):
            if t <= self.tick:
                acc += self.net[t]
        self.L = acc
        return acc

    def next_tick(self, zero_for_one):
        ts = sorted(t for t in self.net if self.net[t] != 0)
        if zero_for_one:
            c = [t for t in ts if t <= self.tick]
            return max(c) if c else None
        c = [t for t in ts if t > self.tick]
        return min(c) if c else None

    def swap_exact_in(self, zero_for_one, amount_in, fee_pips):
        """Returns (amount_out, sqrtP_after). Fee is charged on input, as in v4."""
        rem = Decimal(amount_in) * (Decimal(1_000_000) - Decimal(fee_pips)) / Decimal(1_000_000)
        out = Decimal(0)
        guard = 0
        while rem > 0 and guard < 256:
            guard += 1
            L = Decimal(self.L)
            if L <= 0:
                nt = self.next_tick(zero_for_one)
                if nt is None:
                    break
                self._cross(nt, zero_for_one)
                continue
            nt = self.next_tick(zero_for_one)
            target = sqrt_at(nt) if nt is not None else (Decimal(0) if zero_for_one else Decimal(10) ** 30)
            a = self.sqrtP
            if zero_for_one:
                need = L * (Decimal(1) / target - Decimal(1) / a) if target > 0 else Decimal(10) ** 60
                if rem >= need and nt is not None:
                    out += L * (a - target)
                    rem -= need
                    self.sqrtP = target
                    self._cross(nt, True)
                else:
                    b = Decimal(1) / (Decimal(1) / a + rem / L)
                    out += L * (a - b)
                    self.sqrtP = b
                    rem = Decimal(0)
            else:
                need = L * (target - a)
                if rem >= need and nt is not None:
                    out += L * (Decimal(1) / a - Decimal(1) / target)
                    rem -= need
                    self.sqrtP = target
                    self._cross(nt, False)
                else:
                    b = a + rem / L
                    out += L * (Decimal(1) / a - Decimal(1) / b)
                    self.sqrtP = b
                    rem = Decimal(0)
        return out, self.sqrtP

    def _cross(self, tick, zero_for_one):
        net = self.net.get(tick, 0)
        self.L += -net if zero_for_one else net
        self.tick = tick - 1 if zero_for_one else tick


def replay_and_validate(pool_id, init_sp, init_tick, events, label=""):
    st = PoolState(init_sp, init_tick)
    liq_ok = liq_bad = out_ok = out_bad = 0
    errs = []
    for e in events:
        if e["t"] == "ML":
            st.apply_ml(e["lo"], e["hi"], e["dl"])
            continue
        # validate replayed active liquidity against the event's reported value
        st.recompute_active()
        if e["L"] > 0:
            rel = abs(st.L - e["L"]) / e["L"]
            if rel < Decimal("0.000001"): liq_ok += 1
            else:
                liq_bad += 1
                if len(errs) < 3:
                    errs.append(f"liq replay {st.L} vs event {e['L']} (rel {rel:.2e})")
        # validate simulated output against the event's reported amounts
        zfo = e["a0"] < 0        # caller-centric: a0<0 means token0 paid IN
        amt_in = abs(e["a0"]) if zfo else abs(e["a1"])
        amt_out_actual = abs(e["a1"]) if zfo else abs(e["a0"])
        snap = PoolState(int(st.sqrtP * Q96), st.tick)
        snap.net = dict(st.net); snap.L = st.L
        sim, _ = snap.swap_exact_in(zfo, amt_in, e["fee"])
        if amt_out_actual > 0:
            rel = abs(sim - Decimal(amt_out_actual)) / Decimal(amt_out_actual)
            if rel < Decimal("0.001"): out_ok += 1
            else:
                out_bad += 1
                if len(errs) < 6:
                    errs.append(f"out sim {sim:.4f} vs actual {amt_out_actual} (rel {rel:.4f})")
        # advance to the event's authoritative post-state
        st.sqrtP = Decimal(e["sp"]) / Q96
        st.tick = e["tick"]
    return {"label": label, "pool": pool_id, "swaps": liq_ok + liq_bad,
            "liq_ok": liq_ok, "liq_bad": liq_bad, "out_ok": out_ok, "out_bad": out_bad,
            "errs": errs}


if __name__ == "__main__":
    latest = int(rpc("eth_blockNumber", []), 16)
    FIXTURES = [
        ("FIRE/USDG 50%", "0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43"),
        ("FIRE/USDG 40%", "0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636"),
        ("LICKLET 88%", "0xa39e211f0e5054913ef052f86811689b13fd629b1f5b9e91b4450be184a3ffa9"),
        ("LICKLET 87%", "0x7a63f3834dadc15a2a1b7575be549669b9969d80675db726e3df1eeacac14c7e"),
    ]
    print("v4 STATE REPLAY -- self-validation against event-reported state\n")
    for label, pid in FIXTURES:
        try:
            lg = rpc("eth_getLogs", [{"address": PM, "fromBlock": "0x0", "toBlock": "latest",
                                      "topics": [INIT, pid]}])
            if not lg:
                print(f"{label}: no Initialize found (pool id may be wrong) -- skipped")
                continue
            d = lg[0]["data"][2:]; w = [d[i:i + 64] for i in range(0, len(d), 64)]
            sp0, tick0 = int(w[3], 16), i24(w[4])
            ib = int(lg[0]["blockNumber"], 16)
            ev = fetch_pool_events(pid, ib, latest)
            r = replay_and_validate(pid, sp0, tick0, ev, label)
            print(f"{label}")
            print(f"  events: {len(ev)}  swaps validated: {r['swaps']}")
            print(f"  active-liquidity match: {r['liq_ok']}/{r['swaps']}"
                  f"   output match (<0.1% rel): {r['out_ok']}/{r['out_ok']+r['out_bad']}")
            for e in r["errs"]:
                print(f"    ! {e}")
            print()
        except Exception as ex:
            print(f"{label}: FAILED {str(ex)[:140]}\n")
