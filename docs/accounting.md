# Accounting

## Funding

FundingEscrow holds yvUSDC-1. `purchase(week, amount)` withdraws exact USDC to the fixed payee with zero permitted loss. Each future programme week can be paid once. The 100,000 USDC lifetime limit is denominated in assets; yield and recovered shares never restore spending capacity.

## Claim-week settlement

A sale uses `week = block.timestamp // 604800 * 604800`. There is no attempt to identify when each CRV unit was earned. The operating agreement is that Vyper and other authorised claimers leave claims and sales to this automation, which runs regularly and clears pending rewards near each week's end. Small carryover receives the following week's split. Missed periods and independent claims require operational reconciliation before normal sales resume; the contract does not detect those breaches.

`sell(min_out)` captures the current week's share if needed, calls the Curve minter, and measures the increase in the receiver's CRV balance. It pulls and sells **that entire increase**. Existing CRV in either wallet is excluded. Existing escrow crvUSD is excluded by measuring the swap's balance delta. Yearn's fraction stays in escrow and all remaining output returns to Vyper atomically. A failed pull, swap or return rolls back the claim and snapshot as well.

Before START, at or after END, and in any unpaid week, Yearn's share is zero and all output returns to Vyper. Pre-start sales clear historical pending emissions without appropriating them. A final pre-end sale minimizes carryover; the post-end cleanup returns its whole output to Vyper under the same claim-week rule. Automation must be stopped and claim responsibility handed back afterward.

## Weekly share

For funded weeks, `checkpoint()` obtains:

```text
Yearn weight = effective_vote.slope * max(effective_vote.end - week, 0)
total weight = GaugeController.points_weight(gauge, week).bias
share = floor(Yearn weight * 1e18 / total weight)
retained = floor(actual crvUSD output * share / 1e18)
```

A zero total requires zero Yearn weight and gives zero share. Division rounding goes to Vyper. Each funded week stores only a captured flag and its fixed-point share; the Snapshot event records both original weights.

The [Curve controller](https://github.com/curvefi/curve-dao-contracts/blob/master/contracts/GaugeController.vy) overwrites the latest voter record immediately when a vote changes, although the new vote takes effect next week. Capture before a revote. Public checkpointing is idempotent and works at the exact boundary, with no claim and while paused. Automatic capture requires `last_user_vote < week`. The manager can fill an unset funded snapshot through `set_missing_weight(week, weight)`; the contract reads the historical denominator and bounds the supplied weight. This is a trusted assertion, not a storage proof.

## Quotes and records

`quote()` uses the gauge's `claimable_tokens_write` and the fixed pool's `get_dy(2, 0, amount)`. Use eth_call so the gauge checkpoint remains simulated. Curve handles its own emission cap, annual reductions and checkpoint rounding. The escrow stores no accrual, inflation rate, emission cap, inventory, sale cursor or lifetime proceeds totals.

The keeper chooses a positive `min_out` from the full-claim quote. Cadence controls size; the minimum trade threshold is bypassed near rollover and at final cleanup. Pauses, gas limits, failed transactions or downtime can prevent a closing sale. The timing convention is best effort, not a contract guarantee.

Purchased and Sold events provide the report ledger. Rows describe **claim-week settlements**, not precisely reconstructed earnings or a proof that every reward was collected. Closed means the calendar week ended. Current balances come from token/vault reads, and withdrawals remain distinct from total retained proceeds. The manager can recover any ERC20, including while paused; neither escrow enforces grant milestones.
