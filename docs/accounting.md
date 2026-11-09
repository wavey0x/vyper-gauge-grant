# Accounting

## Funding

FundingEscrow holds yvUSDC-1 shares. Payments and the immutable 100,000 limit use USDC's six-decimal units. `purchase(week, amount)` withdraws exactly that USDC amount to the fixed payee with zero permitted loss. Each future programme week can be paid once. Yield and recovered assets never restore spending capacity.

## Sync and weekly attribution

Weeks begin Thursday 00:00 UTC: `week = timestamp // 604800 * 604800`. The frozen share is:

```text
Yearn weight = effective_vote.slope * max(effective_vote.end - week, 0)
total weight = GaugeController.points_weight(gauge, week).bias
share = Yearn weight / total weight
```

The [Curve controller](https://github.com/curvefi/curve-dao-contracts/blob/master/contracts/GaugeController.vy) stores only the latest voter record. A new vote overwrites that record immediately and takes effect the following week. Automatic capture requires `last_user_vote < week`; missing history never silently becomes a zero share. Public `sync()` captures the current funded week even with zero accrual, and remains available while sales are paused. Call it before a revote and near rollover.

`set_missing_weight(week, yearn_weight)` lets the manager fill only an unset snapshot. The contract obtains the historical denominator, requires a funded programme week and enforces `yearn_weight <= total`. The CLI reads the voter immediately before the boundary block and prepares the value for review. This is a **trusted manager assertion**, not a storage proof. Recovery is recorded by `Snapshot.manual`; ordinary reports do not independently certify it.

Deployment captures the gauge's cumulative accrual baseline. Only newly accrued CRV in paid weeks becomes sale entitlement. Unrelated balances, legacy claims and unfunded weeks create none. Independent minting does not change that entitlement. Vyper must leave the programme's CRV available for the permitted pull; fungible tokens cannot be distinguished by origin within its wallet.

One internal `_sync()` is used by both public `sync()` and permissioned `sell()`. Public sync returns total available CRV; an `eth_call` can preview it without sending a transaction. Within one earning week, use the observed accrual delta. Across weekly or annual boundaries, preserve conservative per-segment attribution because third-party gauge checkpoints introduce integer floors:

```text
k = inflation_rate * gauge_relative_weight
upper = (k * dt) // 1e18
lower = (k // 1e18) * dt
eligible = min(lower, max(cap_remaining_at_anchor - sum(prior upper), 0))
```

The observed aggregate delta must fit the capped bounds. Unassigned rounding stays with Vyper and is not moved into another week. Uncapped uncertainty is less than one CRV wei per elapsed second. Annual reductions use the deployed gauge's coefficient; rate and epoch storage change only when an annual boundary is crossed.

Catch-up is bounded to 16 weeks. A final sync crossing programme end closes accrual; remaining inventory can still be sold afterward. Reaching the gauge's emissions cap is distinguished from an explicit kill, allowing the last accrual to be recorded even if another caller reached the cap first. Cap changes, unexplained rate changes, explicit kills and excessive downtime stop automation; manager recovery remains available. There is no arbitrary override for earned CRV.

## Sale settlement

The operator chooses amount, `min_out` and deadline. Only approved keepers may sell, through the immutable pool `0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14`, CRV index 2 to crvUSD index 0. There is no oracle floor, trade-size policy or cooldown in the contract.

`sell()` syncs first and checks the exact requested amount against available inventory. It claims when necessary, pulls CRV from Vyper and measures the swap's crvUSD balance delta. Old escrow balances never enter the split.

A small chronological cursor identifies the oldest unsold earnings. A single trade may consume several weeks. Allocate output in proportion to each week's CRV input, with the final portion receiving the transaction's division remainder; split each portion with `yearn = portion_output * yearn_weight // total_weight`. Each portion emits `Sold`. The sum is retained for Yearn, and the rest is returned to Vyper in the same transaction. All accounting, claims, transfers and the swap revert together on failure. A one-wei old CRV remainder can be consumed alongside newer earnings; no dust threshold is needed.

## Stored state

Each week stores its immutable vote weights, a captured flag (zero Yearn weight is valid), and remaining CRV. There are no separate earned/sold/output/retained counters. Events provide those report totals. The aggregate available balance enforces sale limits; the accounting and sale cursors prevent duplicate attribution and reselling. The gauge cap is immutable. Sync skips unchanged accrual, inventory and inflation writes, and sell commits aggregate inventory once after syncing.

Balances are read directly. Reports sum finalized purchase, accrual, sale and recovery events and check spending and CRV conservation. Proceeds earned remain distinct from proceeds currently held after manager withdrawals. The manager may recover any ERC20 to itself, including while paused; neither escrow independently enforces grant milestones.
