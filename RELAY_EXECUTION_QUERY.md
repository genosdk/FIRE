# FIRE origin-side route construction on Robinhood Chain — query for Relay / 0x

Prepared from public chain data. Every figure is independently verifiable from
the order IDs and transaction hashes given.

**Read *Current status* first.** The behaviour described was observed
2026-07-19 to 2026-08-09. As of 2026-08-10 it does not reproduce on live quotes,
and the pools involved are close to dormant.

## The question

> Why was the FIRE → ETH → USDG route absent from the candidate route
> construction in these 20 observed orders, despite the selecting infrastructure
> having previously executed against the canonical FIRE/ETH pool, and despite the
> reconstructed route producing more USDG in 18 of 20 cases?

This is deliberately narrower than an earlier version of this document. Our own
analysis shows the routers generally chose the *right FIRE/USDG pool*. The open
question is about the candidate route set, not the venue decision.

## What happened

Between 2026-07-19 and 2026-08-09, 20 Relay intents were originated on Robinhood
Chain by users supplying FIRE (`0x43eea882b845a8493152ebc55cf30ae9281b02d5`).

In all 20, the origin-side swap sold the user's FIRE **exclusively through
extreme-fee Uniswap v4 pools** — 40%, 45%, 50% and in one case 85% — and none of
the 20 routes touched the canonical ETH/FIRE 0.30% pool at any hop.

```
orders with no canonical ETH/FIRE@3000 leg:  20 / 20

extreme-fee tiers used across the 20 routes:
   85.00%   1 hop
   50.00%  12 hops
   45.00%   2 hops
   40.00%  10 hops
```

Across the broader population of 107 transactions touching these pools, the
canonical pool *does* appear in 57. It is absent from every Relay origin route.

## Finding 1 — direct FIRE/USDG venue selection was generally sound

Every FIRE/USDG hop in all 20 orders was replayed at its exact pre-swap state
and compared against every simultaneously live FIRE/USDG pool.

```
DIRECT_OPTIMAL      17 / 20 orders    router took the best available FIRE/USDG pool
DIRECT_SUBOPTIMAL    3 / 20
DIRECT_SEVERE        0 / 20
worst single-hop shortfall              ~2.2%
```

All 13 FIRE/USDG pools are hookless, so these are high-confidence
counterfactuals rather than estimates. In several orders the extreme-fee pool
chosen was better than the next-best venue by a wide margin — for
`0xcd5a6a7752…` by 48%, for `0xd87fc3352f…` by 27%.

**The extreme nominal fees are largely an optical illusion.** These pools carry
correspondingly extreme price dislocation, and at the moment of execution they
frequently were the best direct FIRE/USDG venue available.

## Finding 2 — the omitted ETH-bridged route was better in 18 of 20 orders

Reconstructing FIRE → ETH (canonical 0.30%) → USDG at exact pre-order state,
using each order's actual aggregate FIRE input:

```
orders reconstructed              20 / 20
canonical route better            18 / 20
median difference                     -2.69%
best / worst                    +0.51% / -23.56%

total actual proceeds             99.1740 USDG
total reconstructed canonical    102.4923 USDG
difference                         3.3183 USDG
VOLUME-WEIGHTED difference           -3.24%
```

Two orders were **better** through the observed route: `+0.51%` and `+0.16%`.

Largest individual gaps:

| tx | FIRE in | actual USDG | canonical USDG | diff | shortfall |
|---|---|---|---|---|---|
| `0x0e8761a434…` | 6,844.64 | 2.296555 | 3.004361 | −23.56% | 0.7078 |
| `0xca79f4c118…` | 2,687.15 | 1.913554 | 2.050465 | −6.68% | 0.1369 |
| `0x1b7c5c26aa…` | 4,013.43 | 2.792735 | 2.989913 | −6.59% | 0.1972 |
| `0xd87fc3352f…` | 17,043.09 | 9.310182 | 9.842413 | −5.41% | 0.5322 |
| `0x64fa0aa1c3…` | 5,201.55 | 2.401443 | 2.527768 | −5.00% | 0.1263 |

Full detail including `orderId` and user address is in
`data/analysis/FIRE_CANONICAL_COUNTERFACTUAL.csv`.

## Finding 3 — the omitted route's components were demonstrably usable

```
canonical FIRE/ETH used by the selecting sender BEFORE the order   20 / 20
chosen ETH/USDG bridge ever used by that sender                    19 / 20
```

Every router that selected one of these routes had already executed against the
canonical FIRE/ETH pool prior to the order in question. Venue non-support does
not explain the omission.

## Who selected the route

Relay does not route the origin swap itself. Its advertised origin swap sources
on chain 4663 are `weth`, `0x`, `magpie`, `kyberswap`, `pancakeswap` — Uniswap is
not among them.

```
0x1d4b86491ec211257cbedd77a4380a7494624eff  RobinHoodSettler (0x)     11 orders
0x8f10b468b06c6fd214b65f87778827f7d113f996  router (unlabelled)        4
0xaa61254627b7392b0bc922097b10eb0587db2be7  RobinHoodSettler (0x)      2
0x39b38686a19836ac10162c490e4558e120cbbe5f  RobinHoodSettler (0x)      2
0x8876789976decbfcbbbe364623c63652db8c0904  Uniswap UniversalRouter    1
```

`RobinHoodSettler` is a 0x Protocol Settler deployment, and a live Relay quote's
deposit calldata embeds `0x AllowanceHolder`, not the Uniswap PoolManager. So
pool-level selection was made by **0x**, not by Relay and not by Uniswap.

For context, across a 4,598-pool sample of routed orders chain-wide, 0x Settler
was beaten by a better same-pair pool **less** often than Uniswap's own Universal
Router (1.57% vs 2.89%). This is not a 0x-specific defect.

## Questions

1. **(Relay)** For the 20 `orderId`s in the attached CSV, what origin-side output
   was quoted, and what did the user receive on the destination side?
2. **(0x)** Why was a FIRE → ETH → USDG candidate route absent from these route
   constructions, given prior execution against the canonical FIRE/ETH pool?
3. **(Relay)** Is any quoted-versus-delivered difference borne by the user, or
   absorbed by the solver?

## Methodology

The original observational benchmark in an earlier version of this document —
which reported −8.59% to −9.13% volume-weighted and an ~11.86 USDG shortfall —
**has been superseded and should not be cited.** It matched neighbouring realized
swaps at differing sizes and times, and overstated the gap by roughly 2.7×.

The current figures come from exact historical-state reconstruction:

- Each order's **actual aggregate FIRE input** is used, not per-hop amounts.
- Pool state is taken **immediately before the order** at exact
  `(blockNumber, transactionIndex, logIndex)` ordering.
- The swap model reproduced historical outputs at **>99.5% accuracy**
  (1871/1880 canonical swaps, 33483/33495 and 3282/3284 on the two bridges,
  all within 0.5%; and 201/201 exactly on the direct FIRE/USDG pools).
- The bridge comparison uses the **best executable single route among the three
  most active sub-1% ETH/USDG v4 venues**. A broader route search or split
  routing could only improve the counterfactual, so **the 3.24% figure is
  conservative**.
- ETH and WETH are treated as 1:1 economically; the wrapping step is preserved
  explicitly in the reconstruction output.

## Current status (2026-08-10) — does not reproduce live

**Live quotes are not worse than canonical.** Relay's quoted origin-side USDG
was compared against exact same-state v4 Quoter pricing of the canonical route
for 12 sizes from 100 to 100,000 FIRE. Relay was **better in 12 of 12**, by
+0.19% to +0.26%.

**The pools are close to dormant.** In the most recent 400,000 blocks (~11 hours)
the 50% and 40% pools saw one swap each.

## Scale

FIRE trades near 0.00073 USDG. The 20 orders total **99.17 USDG** of user
proceeds, individual orders ranging 1.9 to 21.4 USDG, against a reconstructed
shortfall of **3.32 USDG**. This is a routing-behaviour question, not a
material-loss event.

## Users were credited correctly

For all 20 orders the `RelayErc20Deposit` depositor equals the account that
supplied the FIRE, verified to the wei against ERC-20 `Transfer` logs. No
proceeds were withheld at the Depository.

## Conclusion

The evidence does **not** show that routers generally selected the wrong
FIRE/USDG pool. It shows that the direct pool choices were usually economically
rational, while an ETH-bridged route that appears economically superior was
absent from the observed route construction.

On-chain evidence cannot establish **why** that route was omitted, or whether it
was considered and rejected by the router for reasons not visible on-chain.

## Reproduction

```
scripts/27_state_replay.py                  event-sourced v4 state + swap math
scripts/30_fire_exact_direct_replay.py      hop-level direct venue comparison
scripts/31_fire_canonical_counterfactual.py exact FIRE -> ETH -> USDG counterfactual
```

Data: `data/analysis/FIRE_CANONICAL_COUNTERFACTUAL.csv`,
`FIRE_EXACT_HOP_REPLAY.csv`, `FIRE_EXACT_ORDER_CLASS.csv`,
`FIRE_RELAY_ORDERS.csv`.
