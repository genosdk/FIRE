# Superseded outputs — DO NOT USE

These CSVs were produced before the Uniswap v4 `Swap` sign convention was
corrected. They assume amounts are pool-centric (`positive = token enters pool`).

The correct convention is **caller-centric**:

    amount < 0  ->  caller pays that token INTO the pool
    amount > 0  ->  caller receives that token FROM the pool

Verified against physical ERC-20 `Transfer` logs, matching to the wei. Example
(`0x097b2738e51cb10e…`, currency0 = FIRE):

    Swap event  amount0(FIRE) = -4144008340027574371295
    Actual      FIRE into PoolManager = 4,144.008340
                USDG out of PoolManager =    3.023953

In these files every `direction` label is reversed and the `fire_in`/`fire_out`
and `usdg_in`/`usdg_out` fields are swapped. Fee accrual derived from them was
computed on the output side rather than the input side.

Retained for provenance. Current versions live in `data/swaps/` and
`data/analysis/`.
