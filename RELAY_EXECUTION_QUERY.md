# Relay origin-side execution query — FIRE on Robinhood Chain

Prepared for sending to Relay. Everything below is derived from public chain data
and is independently verifiable from the order IDs and transaction hashes given.

## Summary

Between 2026-07-19 and 2026-08-09, 20 Relay intents were originated on Robinhood
Chain by users supplying FIRE (`0x43eea882b845a8493152ebc55cf30ae9281b02d5`).

In all 20, the origin-side swap sold the user's FIRE **exclusively through
extreme-fee Uniswap v4 pools** — 40%, 45%, 50% and in one case 85% — and none of
the 20 routes touched the canonical ETH/FIRE 0.30% pool at any hop.

Measured against contemporaneous, strictly pre-trade canonical-market executions,
17–18 of the 20 received less USDG than that benchmark implies, volume-weighted
−8.6% to −9.1% depending on matching method.

We are not claiming Relay violated a quote — we cannot see the quotes. We are
asking why these routes were selected.

## The question for Relay

1. For the 20 `orderId`s listed below, what origin-side output did Relay quote,
   and what did the user actually receive on the destination side?
2. Why did origin-side routing select 40–85% fee pools when the canonical 0.30%
   ETH/FIRE pool was live and being used by other traders in the same blocks?
3. Is the quoted-versus-delivered difference borne by the user, or absorbed by
   the solver?

## Confirmed on-chain facts

**These are real users, and they were credited correctly.** For all 20 orders the
`RelayErc20Deposit` depositor equals the account that supplied the FIRE (verified
to the wei against ERC-20 `Transfer` logs). Sources are user-like accounts — 25
EIP-7702 delegated wallets and 17 plain EOAs across the wider one-way population,
zero contracts. No proceeds were withheld from users at the Depository.

**No canonical leg, in any of the 20.**

```
orders with no canonical ETH/FIRE@3000 leg:  20 / 20

extreme-fee tiers used across the 20 routes:
   85.00%   1 hop
   50.00%  12 hops
   45.00%   2 hops
   40.00%  10 hops
```

For context, across the broader population of 107 transactions touching these
pools, the canonical pool *does* appear in 57. It is absent from every Relay
origin route.

**Execution versus a pre-trade canonical benchmark.** Benchmark route is
FIRE → ETH via the canonical 0.30% pool, then ETH → USDG via the deep v3
WETH/USDG pool `0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca`, using only
observations strictly earlier than the user's block.

```
spec             N worse      median     volume-weighted
nearest-1        17 / 20     -6.293%              -9.02%
median-3         18 / 20     -4.697%              -8.59%
median-5         17 / 20     -6.334%              -9.13%
```

## Strongest individual cases

Ordered by estimated USDG shortfall. All are negative under all three
specifications with at least two HIGH/MEDIUM-quality matches.

### 1. `0x7ff8e745d14ac0f798fe449114c5cd5dd58790df090b415c407e6218f7b0d407`
```
orderId  0x196fbae81177d0bd0adf2d89f330f04220b2d1b821120ea68317ac11aaf77a55
user     0xb1cc9e0cd3d51a582b7bd66049aee8854fdba408
block    21,879,972    2026-07-28 20:29:47 UTC
sold     59,078.6163 FIRE  ->  21.389005 USDG deposited
route    FIRE/USDG@40%  ->  FIRE/USDG@45%  ->  FIRE/USDG@50%
specs    n1 -12.13% (M)   m3 -12.16% (H)   m5 -11.70% (H)
est. shortfall  ~2.57 USDG
```

### 2. `0x20a5232eb7c5d688b2ec7815e59c6428c7c7c76f1ef6c0101e2f6fc272cb6886`
```
orderId  0x5c278f605caa787d4e374f45f368502b80ca0dc59ef736e11f14acc72dfe2fc4
user     0xa6580ce630320e9e88a32ec5a6fbe154252dd09e
block    22,699,058    2026-07-29 19:18:19 UTC
sold     80,411.1526 FIRE  ->  16.327337 USDG deposited
route    FIRE/USDG@85%  ->  FIRE/USDG@45%  ->  FIRE/USDG@40%  ->  FIRE/USDG@50%
specs    n1 -14.04% (M)   m3 -14.03% (H)   m5 -14.91% (H)
est. shortfall  ~2.34 USDG
```

This is the single clearest case: a user's FIRE was split across four separate
extreme-fee pools, one charging 85%, with no canonical leg.

### 3. `0xcd5a6a7752daf94ef19ddf3ddd47d3c2ebccf65ae9bdd308f72e674c9015221f`
```
orderId  0xa739c08107aa4f14740576ac257d51f1f256f0ce18e3a5851e5257cc37af3a2c
user     0x084dfa381fb739bc14d5dcca2c9466e62d712946
block    24,603,793    2026-08-01 00:20:26 UTC
sold     9,504.3090 FIRE  ->  2.874755 USDG deposited
route    FIRE/USDG@40%
specs    n1 -19.48% (M)   m3 -19.48% (H)   m5 -19.48% (H)
est. shortfall  ~0.56 USDG
```

Largest percentage shortfall, and identical across all three specifications.

### 4. `0x1b7c5c26aa9724794d5b9685cb306f0c9dc05a8811a5d2caf734c6b5a1446268`
```
orderId  0x36f09c51add846100bc080f61523be60a39e98f080a34b30c1081044366fcd6b
user     0x7a09760387b078c9e255467c8c5620e25f8a16af
block    17,054,619    2026-07-23 05:59:24 UTC
sold     4,013.4345 FIRE  ->  2.792735 USDG deposited
route    FIRE/USDG@50%
specs    n1 -15.51% (M)   m3 -15.56% (M)   m5 -15.51% (M)
```

### 5. `0x1347148cb39ca5001db1b7be88e91c25134e8bf2fa4920fdfd6b6ff450fe58ee`
```
orderId  0x9eb883ac1546c736e1fbba67ca4c1727b34eefb0020d7aeea203b05c44084eb5
user     0xfea12f8f88faf484c0c74818f48b6a688c549d2d
block    18,362,149    2026-07-24 18:24:41 UTC
sold     6,169.6966 FIRE  ->  3.792264 USDG deposited
route    FIRE/USDG@50%
specs    n1 -12.51% (M)   m3 -10.29% (H)   m5 -10.30% (H)
```

11 of the 20 orders are negative under all three specifications with ≥2
HIGH/MEDIUM matches, accounting for 64.87 of the 99.17 USDG deposited.

## Why these pools exist at all

Worth stating so the question is not misread as "someone attacked FIRE".

The extreme-fee pools are ordinary hookless v4 pools and are not FIRE-specific —
23 of 116 ETH/USDG pools on this chain also charge ≥10%, up to 100%. Their quoted
prices sit far enough from the main market that the enormous fee still leaves a
profitable band: every closed arbitrage cycle we could measure through them was
profitable gross and net of gas. That mispricing is self-sustaining because the
fee itself widens the no-arbitrage band.

So it is entirely possible for a router to see one of these pools as economically
useful despite the headline fee. What we cannot explain is why *all 20* user
intents took that path and *none* touched the canonical pool.

## Method and limitations

- Benchmark uses only observations strictly earlier than the user's block (no
  look-ahead), matched on block distance and trade size, at top-1 / median-3 /
  median-5.
- It compares *neighbouring realized executions*, not an exact historical quote
  for the user's exact size at the exact pre-trade state. The public RPC retains
  only ~6,200 blocks of state, so a Quoter-based counterfactual is impossible.
- Match quality is graded per row; LOW-confidence rows should not carry weight.
  Confidence mix at median-5: 6 HIGH, 9 MEDIUM, 5 LOW.
- Under a symmetric window that allowed look-ahead, the result was 20/20 negative
  at −10.68% volume-weighted. Removing look-ahead reduced it to 17–18/20 at
  −8.6% to −9.1%. We report the stricter figure.
- The canonical pool's hook holds both swap return-delta permissions but returns
  zero on the FIRE side in 196/196 audited swaps, so canonical `Swap` amounts are
  used directly.

## Reproduction

```
scripts/08_relay_orders.py                  # decode orderIds from deposits
scripts/09_relay_execution_benchmark.py     # symmetric-window benchmark
scripts/11_relay_execution_benchmark_pretrade.py   # pre-trade-only, 3 specs
scripts/10_benchmark_invariants.py          # verification of decoding + arithmetic
```

Data: `data/analysis/FIRE_RELAY_ORDERS.csv`,
`FIRE_RELAY_EXECUTION_BENCHMARK_PRETRADE.csv`.

Setting `RELAY_API_KEY` makes `08_relay_orders.py` populate the quote and
destination-fill columns, which is the missing half of this question.
