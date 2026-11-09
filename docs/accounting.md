# Accounting

## Budget

The funding contract holds yvUSDC-1 shares, but payments and the lifetime 100,000 limit use USDC's six-decimal units. `purchase(week, amount, reference)` withdraws exactly `amount` USDC to the immutable payee with `max_loss=0`, checks the recipient's balance delta, and records the week atomically. Each programme week can be purchased once, before it starts. Programme duration is fixed at deployment, at most 52 weeks. Yield does not increase the budget, and recovering shares does not restore spending capacity.

## Weekly vote share

Weeks start Thursday 00:00 UTC: `w = timestamp // 604800 * 604800`.

```text
Y = effective_vote.slope * max(effective_vote.end - w, 0)
T = GaugeController.points_weight(gauge, w).bias
share = Y / T
```

This is the [VoteMarket lens](https://arbiscan.io/address/0x99EDB5782da5D799dd16a037FDbc00a1494b9Ead#code) bias formula, applied directly on Ethereum. The slope already includes the gauge allocation. Do not multiply by voting power again. The ratio is frozen for the earning week; it is proportional attribution, not a counterfactual estimate of marginal emissions caused by Yearn.

The controller keeps only the latest voter record. A vote submitted in week `w` overwrites that record immediately but takes effect in `w + WEEK`. A fresh snapshot is therefore usable only if `last_user_vote < w`. Missing history reverts; it never becomes a zero Yearn share. Capture shortly after rollover and before replacing votes. A finalized snapshot is immutable.

The manager can fill a missing snapshot using `repair_week`. The script reads voter state immediately before the first block at or after the boundary, prepares the raw slope/end/last-vote values, and hashes its evidence. The contract independently obtains the historical gauge total and enforces `Y <= T`. **The historical voter inputs are trusted manager assertions, not cryptographic storage proofs.** Reconciliation checks repaired snapshots against archival state; that check is detective, not an onchain veto. Vyper must accept this recovery authority along with its revocable allowance.

## CRV earning ledger

Activation claims pre-programme emissions into Vyper's wallet and records the cumulative gauge baseline. Only weeks paid by the funding contract create sale entitlement. Arbitrary wallet balances, old claims, unrelated token transfers and unfunded weeks create none.

`checkpoint()` observes the gauge's actual cumulative accrual, partitions elapsed time at weekly and annual inflation boundaries, and books the oldest earning lots. It is separate from `sell`: the quote always refers to an already-booked exact amount. Within one week, use the measured actual accrual delta. Across weeks, use conservative bounds because third-party gauge checkpoints introduce extra integer floors.

For each constant-rate, constant-weight segment lasting `dt` seconds:

```text
k = rate * gauge_relative_weight
upper = (k * dt) // 1e18
lower = (k // 1e18) * dt
eligible = min(lower, max(remaining_cap_at_anchor - sum(previous upper), 0))
```

Any integer-second partition's accrual lies between `lower` and `upper`. Subtracting earlier upper bounds from the remaining cap prevents assigning more than a segment could actually earn. Funded and unfunded segments both consume the cap. The observed total must lie within the aggregate capped bounds. The tiny unassigned rounding remainder stays with Vyper; it is not carried into a later earning week. Uncapped uncertainty is less than one CRV wei per elapsed second.

The rate reduction is the deployed gauge's `rate * 1e18 // 1189207115002721024` every 365 days. Checkpoints support up to 16 weeks of elapsed time and stop after a final checkpoint crosses programme end. Cap changes, killed gauges, unexplained rate/state changes, or excessive downtime stop new accounting. These are explicit operating stops; there is no arbitrary accrual override. Already-booked entitlements can still be settled, and manager recovery remains available.

## Sale settlement

Only approved keepers sell. The input is bounded by the oldest unsold funded-week entitlement and the configured trade limit. The minter must have minted all accrual through the accounting cursor; minting still goes to Vyper, followed by `transferFrom`. Independently initiated minting does not reset entitlement. Vyper must not independently dispose of CRV already assigned to this programme: tokens are fungible and the contract can constrain quantity, not identify individual minted tokens.

FIFO skips a residual of at most `1e9` CRV wei (one nano-CRV) per week, leaving it with Vyper and visible as unsold inventory. This prevents an unquotable one-wei remainder from blocking every later week. It is never counted as a sale or as proceeds.

The immutable route is pool `0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14`, CRV index 2 to crvUSD index 0. The keeper discounts `quote(amount)` to produce `min_out`; the contract also enforces a floor against `price_oracle(1)`, maximum trade size and cooldown. The pool's own moving-average price is a guard, not an independent price feed.

Only the measured output delta is split. For weekly cumulative output `P` and new output `p`:

```text
yearn_this_sale = ((P + p) * Y // T) - prior_yearn_retained
vyper_this_sale = p - yearn_this_sale
```

Input, swap, accounting and Vyper's return transfer either all succeed or all revert. Old escrow balances and unsolicited deposits never enter the split. Lifetime retained proceeds do not decrease on withdrawals. The manager can recover any ERC20 to itself at any time, including while paused; this contract does not independently enforce TheDAO's grant milestones.
