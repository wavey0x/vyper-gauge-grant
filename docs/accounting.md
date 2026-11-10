# Accounting

## Funding

FundingEscrow holds USDC, yvUSDC-1 or both. `purchase(week, amount)` transfers available USDC to the fixed payee, then withdraws only the shortfall from the vault with zero permitted loss. The payee's USDC balance must rise by exactly `amount`; a failed withdrawal rolls back the earlier transfer and budget writes. Each future programme week can be paid once. The 100,000 USDC lifetime limit is denominated in assets; yield and recovered funds never restore spending capacity. `assets()` reports liquid USDC plus the vault's converted share balance.

Funding has no seller allowance, swap logic or keeper permissions. Only the fixed `0x4444…ce4a` manager can purchase. That manager and governance **ychad.eth** (`0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`, resolved on Ethereum November 10, 2026) can recover any ERC20 to the caller at any time. Share recovery needs no vault redemption and is unaffected by sale pauses or programme dates. Already-paid USDC cannot be clawed back from the payee.

## Claim-week settlement

A sale uses `week = block.timestamp // 604800 * 604800`. There is no attempt to identify when each CRV unit was earned. The operating agreement is that Vyper and other authorised claimers leave claims and sales to this automation, which runs regularly and clears pending rewards near each week's end. Small carryover receives the following week's split. Missed periods and independent claims require operational reconciliation before normal sales resume; the contract does not detect those breaches.

`sell(min_out, expected_week, quoted_crv)` calculates the execution week and rejects a different `expected_week`. It captures that week's share if needed, calls the Curve minter, and measures the increase in the Vyper receiver's CRV balance. It pulls and sells **that entire increase**. Existing CRV in either wallet is excluded. The fixed first pool exchanges CRV for crvUSD; only its returned amount enters the second swap to USDC. Existing escrow crvUSD is left untouched. USDC output is measured by balance delta, excluding any existing balance.

`yearn_receiver` starts as the proceeds contract itself. Yearn's fraction stays here unless management selects another nonzero address through `set_yearn_receiver`. Future sales then forward that fraction atomically; the rest always returns to Vyper. Receiver changes never sweep existing balances, and setting the receiver back to the contract resumes accumulation. A failed pull, either swap, forwarding or return rolls back the entire sale, claim and snapshot.

The fixed pools are [TriCRV](https://etherscan.io/address/0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14#code), indices 2 → 0, and [crvUSD/USDC](https://etherscan.io/address/0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E#code), indices 1 → 0. Pool assets and interfaces were verified on Ethereum and the route is exercised on a mainnet fork. The first hop has no separate minimum. The final hop requires `min_out * actual_minted_crv // quoted_crv` in **6-decimal USDC units**, so the minimum per CRV remains valid if the claim grows. If the larger trade has worse natural price impact, it may revert and need a fresh quote. crvUSD is a transient intermediate, not the settlement asset.

Before START, at or after END, and in any unpaid week, Yearn's share is zero and all output returns to Vyper. Pre-start sales clear historical pending emissions without appropriating them. A final pre-end sale minimizes carryover; the post-end cleanup returns its whole output to Vyper under the same claim-week rule. Automation must be stopped and claim responsibility handed back afterward.

## Weekly share

For funded weeks, `checkpoint()` obtains:

```text
Yearn weight = effective_vote.slope * max(effective_vote.end - week, 0)
total weight = GaugeController.points_weight(gauge, week).bias
share = floor(Yearn weight * 1e18 / total weight)
yearn = floor(actual USDC output * share / 1e18)
```

A zero total requires zero Yearn weight and gives zero share. Division rounding goes to Vyper. Each funded week stores only a captured flag and its fixed-point share; the Snapshot event records both original weights.

The [Curve controller](https://github.com/curvefi/curve-dao-contracts/blob/master/contracts/GaugeController.vy) overwrites the latest voter record immediately when a vote changes, although the new vote takes effect next week. Capture before a revote. Public checkpointing is idempotent and works at the exact boundary, with no claim and while paused. Automatic capture requires `last_user_vote < week`. The manager can fill an unset funded snapshot through `set_missing_weight(week, weight)`; the contract reads the historical denominator and bounds the supplied weight. This is a trusted assertion, not a storage proof.

## Quotes and records

`claimable()` calls the gauge's `claimable_tokens_write` and returns pending CRV. Use eth_call so its checkpoint remains simulated. The separate view `quote(amount)` chains both pools' quotes and returns expected USDC. Zero input returns zero. Both functions are read at the same block by the tooling. Curve handles its own emission cap, annual reductions and checkpoint rounding. The escrow stores no accrual, inflation rate, emission cap, inventory, sale cursor or lifetime proceeds totals.

The keeper chooses a positive `min_out` from the full-claim quote and supplies the quoted CRV and current week. Cadence controls size; the minimum trade threshold is bypassed near rollover and at final cleanup. A delayed sale that crosses the week boundary reverts. A pending nonce must be mined or manually replaced before the bot can submit another sale. Pauses, gas limits, failed transactions or downtime can still prevent a closing sale, so the timing convention remains best effort.

Purchased and Sold events provide the report ledger. Each Sold event records USDC output, Yearn's amount and its receiver. Rows describe **claim-week settlements**, not precisely reconstructed earnings or a proof that every reward was collected. Closed means the calendar week ended. Current balances come from token/vault reads. Yearn's total allocation, automatically forwarded USDC and management withdrawals are reported separately; an external receiver does not make forwarded amounts appear held in escrow. The manager can recover any ERC20, including while paused; neither escrow enforces grant milestones.
