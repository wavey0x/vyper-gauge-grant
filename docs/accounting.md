# Accounting

## Funding

FundingEscrow holds yvUSDC-1. `draw(amount)` calls the fixed vault's `withdraw(amount, recipient, self, 0)`. The vault calculates the shares to burn, rounding up, and sends the requested USDC directly to the recipient. Zero permitted loss means a shortfall reverts. A successful draw emits its amount and recipient; a failed draw records no payment. The escrow relies on the vault's withdrawal semantics instead of duplicating share conversion or checking recipient balances. The recipient defaults to fee burner `0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8`. Only the manager can change it, and zero or the funding contract itself is rejected.

Funding has no programme dates, week calculation, payment mapping or spending cap. Draws can happen fortnightly, be delayed or include additional payments; their timing never changes proceeds eligibility. Yield and additional shares remain available for future draws. `assets()` reports only the vault's converted share balance. Loose USDC is recoverable but is neither drawn nor counted as vault funding.

Funding has no seller allowance, swap logic or keeper permissions. Only the fixed `0x4444…ce4a` manager can draw. That manager and governance **ychad.eth** (`0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`, resolved on Ethereum November 10, 2026) can recover any ERC20 to the caller at any time. Share recovery needs no vault redemption and is unaffected by sale pauses or programme dates. Already-paid USDC cannot be clawed back from the recipient.

## Claim-week settlement

ProceedsEscrow has no configured start or end dates, funding address or payment checks. Every week uses the designated Yearn voter's full weight on the gauge. This is the operating agreement, independent of whether payment has occurred; recovering funding does not stop proceeds. Early termination requires coordination and pausing sales while reconciling.

A sale uses `week = block.timestamp // 604800 * 604800`. There is no attempt to identify when each CRV unit was earned. The operating agreement is that Vyper and other authorised claimers leave claims and sales to this automation, which runs regularly and clears pending rewards near each week's end. Small carryover receives the following week's split. Missed periods and independent claims require operational reconciliation before normal sales resume; the contract does not detect those breaches.

`work(min_out, max_crv)` calculates the execution week automatically. It captures that week's share if needed, calls the Curve minter, and pulls `min(receiver_CRV_balance, max_crv)` from the Vyper multisig. That exact amount is sold; any remainder stays in the multisig for later batches, even when no new CRV is minted. All CRV in the receiver is treated as programme rewards under the operating agreement. Existing CRV held directly by the escrow is excluded. The fixed pool exchanges CRV directly for crvUSD. The split uses the pool's returned output. Existing escrow crvUSD and unrelated tokens such as USDC are untouched.

`yearn_receiver` starts as the proceeds contract itself. Yearn's fraction stays here unless management selects another nonzero address through `set_yearn_receiver`. Future sales then forward that fraction atomically; the rest always returns to Vyper. Receiver changes never sweep existing balances, and setting the receiver back to the contract resumes accumulation. A failed pull, the swap, forwarding or return rolls back the entire sale, claim and snapshot.

The fixed pool is [TriCRV](https://etherscan.io/address/0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14#code), indices 2 → 0 (CRV → crvUSD). Pool assets and interfaces are exercised on a mainnet fork. The swap receives the operator's `min_out` unchanged, in **18-decimal crvUSD units**. The contract does not calculate a price floor or scale the minimum. There is one swap and only CRV is approved to the pool.

Deployment records `DEPLOYED_WEEK` automatically and captures that week's share if the controller still has its effective vote. When the share is zero, sales send all output to Vyper. A vote cast later in the same week affects the next week and cannot change an already captured snapshot. Sales always use their execution week, including the first sale after rollover; `last_sale` controls only the cooldown. There is no automatic end date. Pause or stop sales and coordinate final settlement and handover when the programme ends.

## Weekly share

For each week, `checkpoint()` obtains:

```text
Yearn weight = effective_vote.slope * max(effective_vote.end - week, 0)
total weight = GaugeController.points_weight(gauge, week).bias
share = floor(Yearn weight * 1e18 / total weight)
yearn = floor(actual crvUSD output * share / 1e18)
```

A zero total requires zero Yearn weight and gives zero share. Division rounding goes to Vyper. Each week stores only a captured flag and its fixed-point share; the Snapshot event records both original weights.

The [Curve controller](https://github.com/curvefi/curve-dao-contracts/blob/master/contracts/GaugeController.vy) overwrites the latest voter record immediately when a vote changes, although the new vote takes effect next week. Capture before a revote. Public checkpointing is idempotent and works at the exact boundary, with no claim and while paused. Automatic capture requires `last_user_vote < week`. The manager can fill an unset snapshot through `set_missing_weight(week, weight)`; the contract reads the historical denominator and bounds the supplied weight. This is a trusted assertion, not a storage proof.

## Quotes and records

`claimable()` calls the gauge's `claimable_tokens_write` and returns existing receiver CRV plus pending rewards. Use eth_call so its checkpoint remains simulated. The separate view `quote(amount)` reads the pool quote and returns expected crvUSD (18 decimals). Zero input returns zero. Both functions are read at the same block by the tooling. Curve handles its own emission cap, annual reductions and checkpoint rounding. The escrow stores no accrual, inflation rate, emission cap, inventory, sale cursor or lifetime proceeds totals.

The keeper quotes `min(claimable(), configured_max_crv)` and applies its configured slippage discount offchain. It passes that quoted amount as the transaction's `max_crv`, with no multiplier or growth margin. Additional rewards arriving before execution remain in the multisig. The configured cap defaults to 15,000 CRV. The minimum trade threshold is bypassed in the last five minutes before rollover, but the cap and cooldown still apply; drain a large backlog before that window. A delayed sale crossing the boundary uses the new week's share; there is no week parameter or deadline. A pending nonce must be mined or manually replaced before the bot can submit another sale.

Every successful sale records `last_sale`. All keepers share a cooldown, initially 600 seconds; only the manager can change it, with a hard floor of 600 seconds. Changes apply relative to the last sale without resetting that timestamp. A failed sale does not consume the interval. `canWork()` returns whether sales are unpaused and the interval has elapsed; it is independent of the caller and does not promise that a claim or swap will succeed. Checkpointing remains public during the wait. The keeper skips quoting until ready and can still checkpoint during cooldown. There is no exception to the cooldown at week boundaries.

Drawn and Sold events provide separate report ledgers. Each Drawn event records the USDC amount and recipient; reports derive total spending from these events and retain each payment's block timestamp and transaction. Payments are never assigned to vote weeks. Weekly rows describe **claim-week settlements**, not precisely reconstructed earnings or a proof that every reward was collected. Reporting begins at the deployment week and warns when the current week lacks a snapshot. Each Sold event records crvUSD output, Yearn's amount and its receiver. Closed means the calendar week ended. Current balances come from token/vault reads. Yearn's total allocation, automatically forwarded crvUSD and management withdrawals are reported separately; an external receiver does not make forwarded amounts appear held in escrow.

The manager or governance (ychad.eth) releases held proceeds with `withdraw(token, amount, recipient)`, including while paused. Either can pay TheDAO directly or send any other ERC20 to a chosen address. Zero and the escrow itself are rejected as recipients. The `Withdrawn` event records the token, amount and recipient; only crvUSD withdrawals contribute to the dashboard's withdrawn total. A withdrawal does not change `yearn_receiver` or previously recorded earnings. Neither escrow enforces grant milestones.
