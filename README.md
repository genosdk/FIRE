# FIRE / Robinhood Chain — extreme-fee Uniswap v4 pool investigation

Reconstruction of two high-fee Uniswap v4 FIRE/USDG pools on Robinhood Chain, the
flow routed through them, and the execution quality received by users whose FIRE
was sold through them.

Everything here is derived from public chain data via Robinhood Chain's JSON-RPC
(`https://rpc.mainnet.chain.robinhood.com`) and Blockscout's raw-trace endpoint
(`https://robinhoodchain.blockscout.com`). No archive node is required — see
*Constraints* below.

Observation window: **2026-07-19 → 2026-08-09 UTC**.

## Subjects

| | 50% pool | 40% pool |
|---|---|---|
| PoolId | `0xdfbdc88db73b4290a0f02daf53a95c5eb59ceceb7f080e08476d04ee776eee43` | `0x0e6ef141436d1b1948593ce40268b6ffc56870929e83922c15a4a5d64f5b4636` |
| Created | block 13,489,232 (2026-07-19 02:39:43) | block 14,117,871 (2026-07-19 20:12:12) |
| Creation tx | `0x88662c6e…673f80c35` | `0x8689ba10…6bfb29268` |
| fee / tickSpacing | 500000 (50.00%) / 10000 | 400000 (40.00%) / 8000 |
| hooks | none (`0x0`) | none (`0x0`) |
| Creator + LP | `0xe49193acd219557a150e35ccc2a66f89c42482e6` | `0xc6312da2663570a2aee12b27a0c6da4683339acd` |

Both pair FIRE (`0x43eea882…02d5`, 18 dp) as currency0 with USDG
(`0x5fc5360d…d168`, 6 dp) as currency1.

Canonical reference market: PoolId
`0x2276440d38b33394989f7819f63b1df5ed62e48192706c172cabef1480547efd` —
native ETH / FIRE, fee 3000, tickSpacing 60, hook `0xe3fa8fa0…50cc`.

## Pipeline

Scripts are numbered in dependency order. Each writes into `data/`.

| script | produces |
|---|---|
| `01_lp_history.py` | every `ModifyLiquidity` event for both pools |
| `02_swap_history.py` | every `Swap` event for both pools |
| `03_correlation.py` | swap→poke timing correlation, LP fee accrual |
| `04_hook_delta_audit.py` | canonical hook return-delta audit |
| `05_route_classify.py` | input token + full v4 route per suspect tx |
| `06_cycle_profitability.py` | closed-cycle detection and gross/net profit |
| `07_one_way_trace.py` | economic source/recipient for one-way routes |
| `08_relay_orders.py` | decoded Relay `orderId` + credited depositor |
| `09_relay_execution_benchmark.py` | actual vs synthetic canonical benchmark |
| `10_benchmark_invariants.py` | verification of 09's decoding and arithmetic |

Dependencies: `eth-abi`, `eth-utils`, `eth-hash[pycryptodome]`. The hashing
backend is required — `eth_utils.keccak` raises `ImportError` without it.

## Findings

**Liquidity.** 298 `ModifyLiquidity` events across 28 distinct position NFTs and
12 operator addresses; 6 operators run positions in both pools. 52% of events are
zero-delta "pokes" (fee-realization candidates — the event carries no amount, so
the quantity collected is not observable from logs alone). Poke timing is
elevated 4.8–5.7× over a uniform-random baseline at one hour, but **zero** pokes
occur in the same block as a swap, indicating polling rather than atomic capture.

**Protocol fee.** Swap `fee` is not constant. It rises 500000→500500 (50% pool,
block 21,644,109) and 400000→400600 (40% pool, block 21,879,972), exactly matching
`protocolFee + lpFee × (1 − protocolFee/1e6)` with a protocol fee of 1000 pips.

**Chain context.** Extreme-fee pools are not FIRE-specific. Of 116 ETH/USDG pools,
23 (19.8%) charge ≥10%, up to 100%. This weakens any "FIRE was targeted" reading.

**Canonical hook.** The hook holds `BEFORE_SWAP_RETURNS_DELTA` and
`AFTER_SWAP_RETURNS_DELTA`, so it *can* rewrite swap amounts. Audited over 200
canonical swaps (196 cleanly isolated): **196/196 match physical FIRE movement to
the wei**, both directions. Canonical `Swap` amounts are therefore safe to use
directly for the FIRE side.

**Who trades here.** Of 107 transactions touching the suspect pools, only 20 are
closed arbitrage cycles (9 single-asset + 11 ETH→WETH round trips). All are
profitable gross and net of gas. 78 are one-way routes. Of the 42 FIRE-input
one-way routes, the FIRE source is a user-like account in **100%** of cases
(25 EIP-7702 delegated wallets, 17 plain EOAs, zero contracts).

**Relay intents.** 20 of those transactions are origin legs of Relay cross-chain
intents. The `RelayErc20Deposit` credited depositor equals the FIRE-supplying
account in **20/20** cases — users are not having proceeds withheld.

**Execution quality.** For those 20 orders, actual USDG deposited versus a
size- and time-matched synthetic canonical route (FIRE→ETH via canonical,
ETH→USDG via the deep v3 WETH/USDG pool):

```
Actual worse than benchmark:  20 / 20
Median actual-vs-benchmark:   -4.443%
Aggregate shortfall:          11.857090 USDG on 111.031047 benchmark  (-10.68% volume-weighted)
  HIGH/MEDIUM matches only:   11.470032 USDG on  97.970168            (-11.71%)
  HIGH matches only:          10.516030 USDG on  78.686893            (-13.36%)
```

100% of the FIRE in all 20 orders crossed a ≥10% fee pool (256,773 FIRE total) —
the shortfall is not an artifact of partial routing. The effect strengthens as
match quality improves, which is the opposite of what matching noise would
produce.

This does **not** establish that any quote was violated. Relay's quoted versus
delivered amounts require the authenticated Requests API and are not included here.

## Constraints and caveats

- **Not an archive node.** State retention is ~6,200 blocks (~10 min at 0.1 s
  blocks), so historical `eth_call` — and therefore Quoter-based counterfactuals —
  is impossible. Logs, receipts and traces are fully retained; every result here
  is built from those.
- **The benchmark is observational.** It compares realized executions at differing
  sizes and times, not same-block quotes. Per-row `*_best_size_ratio`,
  `*_nearest_block_distance` and `benchmark_confidence` qualify each comparison.
  4 of 20 rows are LOW confidence.
- **Bridge log gaps.** 8 chunked `eth_getLogs` calls on the busy v3 pool failed
  after retries, thinning candidates for 4 orders.
- **`*_match_txs` columns identify transactions, not log indices.** A single
  transaction can contain several eligible and ineligible swaps on the same pool,
  so those columns do not uniquely reproduce the selected observation.
- **Population scope.** Everything is drawn from transactions touching the 40% and
  50% pools. At least four further extreme-fee FIRE/USDG pools appear inside these
  routes (45%, 85%, 99.123%, 50.1%), so this is not a census.

## Corrections made during the investigation

Two decoding errors were found and fixed. Both are recorded because they changed
conclusions, not just numbers.

1. **v4 `Swap` sign convention.** Amounts are caller-centric (negative = caller
   pays into the pool), not pool-centric. The initial pass had this inverted,
   which reversed every direction label and swapped the `*_in`/`*_out` fields.
   Corrected counts: **112 sells, 77 buys** (not 77/112). Fixed in
   `02_swap_history.py`; superseded outputs are in `data/superseded/`.
2. **Decimals in cycle input.** Token *symbols* were passed to an
   address-keyed decimals lookup, silently defaulting USDG to 18 dp and producing
   returns in the trillions of percent. Fixed in `06_cycle_profitability.py`.

`data/superseded/` holds the direction-inverted CSVs, retained for provenance
only. Do not use them.
