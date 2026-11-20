# Operations

These notes assume Yearn's manager Safe, ychad.eth and Vyper's multisig act honestly. They list the operational steps that keep the deployed contracts working as intended and the mistakes to avoid.

## Keeper schedule

Run `ops/run.sh` and `ops/run.sh funding-keeper` every five minutes from the same host, signer and output directory. The wrapper records the cron start time in `KEEPER_TICK`, which tells the keeper which five-minute pass it is running for. A run that starts more than 30 seconds early or 60 seconds late, or a direct `vyper-grant keeper` run, is unscheduled and never makes the closing sale.

Mainnet blocks land one second before each five-minute mark, so the last block of a week is at W−1 and the first at W+11, where W is Thursday 00:00 UTC. With the default 600 second cooldown:

| Pass (UTC) | Latest block | Proceeds keeper | Funding keeper |
|---|---|---|---|
| Wed 23:40 | W−1201 | Ordinary sale if ready; lands about W−1189 | Pays if due |
| Wed 23:45 | W−901 | Holds | Pays if due |
| Wed 23:50 | W−601 | Holds | Waits |
| Wed 23:55 | W−301 | **Closing sale**; expected to land about W−289, inside the week | Waits |
| Thu 00:00 | W−1 | Sends nothing | Waits |
| Thu 00:05 | W+299 | Records the new week's snapshot if due | Pays a due or overdue instalment |
| Thu 00:10 | W+599 | First ordinary sale of the new week | — |

The rules behind the table:
- **Closing sale.** Only the scheduled 23:55 pass waives `min_trade_crv`, and only with a block at most 36 seconds old. The batch cap, allowance, cooldown and dust rules still apply.
- **Hold.** Any other sale, including a retained batch, needs more than `cooldown + 330` seconds left on the wall clock. That keeps the cooldown clear for the closing sale. A held pass records a missing snapshot, unless the hold only appears at the final check. With a 1,200 second cooldown, the passes from 23:35 to 23:50 hold.
- **Boundary.** Nothing is sent in the last 36 seconds of a week, by the scheduled 00:00 pass, or while the latest block and the wall clock are in different weeks. The keeper checks again before broadcast, including the current cooldown, so a pass that stalls past a limit sends nothing.
- **Gas.** Every proceeds transaction gets 1,000,000 extra gas, because the first controller call of a week pays the weekly update, which costs about 892,000 more. Unused gas isn't charged, but the signer's ETH must cover the full gas limit at the capped fee.
- **Lock.** Both jobs share one lock because they share one signer. A pass waits up to 60 seconds for the other, then reports `busy`.
- **Funding.** The funding keeper starts nothing from 23:50 until the first block of the new week, or while the latest block and the wall clock disagree on the week. A payment already pending from an earlier pass can still hold the nonce at 23:55, which shows as `pending nonce`.
- **Explicit checkpoint.** `ops/run.sh checkpoint` ignores the sale schedule, so it can capture a new week's snapshot from the first block of that week. It only waits while the latest block is still in the previous week.

A closing sale can still land after the boundary if blocks are missed or inclusion is delayed. Its CRV is then split at the new week's share. Alert when a week ends without a closing sale, and reconcile any material rollover with Vyper.

## Keeper statuses and alerts

The keeper prints one JSON status per pass.

| Status | Meaning | Exit |
|---|---|---|
| `sale`, `retain`, `checkpoint`, `funding payment` | Prepared or sent | 0 |
| `cooldown`, `hold`, `below minimum`, `not due`, `boundary`, `closing window`, `dust` | Nothing to do this pass | 0 |
| `allowance exhausted` | Vyper's CRV allowance to the escrow is zero | 0 |
| `busy`, `pending nonce` | Another pass or transaction is in flight | 0 |
| `gas limit`, `low balance`, `paused` | Fee cap reached, signer needs ETH, or sales are paused | 0 |
| `clock skew` | The latest block is more than 5 seconds ahead of the host clock | 0 |
| `simulation failed` | A sale reverted in preparation. If `fallback` is `checkpoint`, a due snapshot was prepared (dry run) or sent (`tx`); confirm its receipt. | 3 |
| `checkpoint failed` | The snapshot also reverted, for example `missing vote history` | 3 |

Transport and configuration errors exit 1. `ops/run.sh` keeps the keeper's exit status even when the report fails.

Alert on:
- any nonzero exit;
- `allowance exhausted` as the status or in the `sale` field, and `mint_enabled: false` in any status;
- `low balance` or `clock skew`, at once;
- `busy` or `pending nonce` across several passes;
- a week that ends without a closing sale.

## Yearn

- **Votes.** Before any vote change on this gauge, put the public `checkpoint()` **first** in the same Safe batch. A vote cast before the week's snapshot blocks automatic capture and every sale that week, until the manager runs `set-missing-weight`.
- **Manual weights.** They are permanent. Filling a past week doesn't change sales already settled.
- **Historical rewards.** Rewards earned before Yearn's first effective vote belong to Vyper. Sell them while the current week's share is still zero. A vote cast during week W applies from W+1. Before starting, refresh the share, claimable rewards, permissions and allowance; afterwards, reconcile the `Sold` events and any leftover CRV.
- **Sale settings.** Keep `sell_vyper: true` unless Vyper asks otherwise. Don't raise `max_crv` or `slippage_bps` without checking price impact on the route.
- **Keeper key.** A leaked key cannot redirect proceeds: Vyper's USDC always goes to Vyper's multisig, Yearn's to `yearn_receiver`, and retained CRV stays in the escrow for Vyper alone. It can sell what is approved at a poor price, or delay sales.
  - The manager can pause the escrow or revoke the keeper.
  - Vyper can revoke the allowance.
  - Keep a modest allowance and little ETH on the key. The ETH balance is a weak limit, because anyone can fund the address.
  - With the optional keeper guard, every sale must return at least 97.5% of Chainlink's CRV/USD value, so a larger allowance is low-risk. The guard must be the only keeper; `setup` disables the others, and `doctor` flags the bot if it's still a direct keeper. Watch for the guard reverting with `stale` if the feed stops updating.
- **Funding.**
  - Batch `fund_round` with a vote only when `next_week == 0`; otherwise the batch reverts.
  - Leave headroom for share rounding and vault liquidity. Instalments don't reserve shares, so a loss or illiquidity can leave a payment due until it is retried. An overdue round can pay both instalments in separate calls.
  - Change the recipient well before a Thursday payment, because anyone can trigger a due payment.
  - `recover` of the vault token already cancels unpaid instalments. Build it from a balance read after any separate cancellation has confirmed.
- **Replacing an escrow.** Deploy in a week Yearn hasn't voted in yet. Then revoke the old escrow's keepers, and have Vyper revoke its allowance and mint permission.

## Vyper

- **Keep the CRV allowance and mint permission on** for the whole programme.
  - With minting off, sales still take the multisig's existing CRV, but new rewards stay in the gauge.
  - Only a zero allowance stops pulls.
  - Either change moves CRV into a later week's share.
- **Top up the allowance early,** while at least one batch remains. CRV only changes an allowance to or from zero, so send `approve(escrow, 0)` and then `approve(escrow, X)` in one batch. X is the new total, not an increment.
- **Mint permission.** `toggle_approve_mint` flips the permission instead of setting it. Build the toggle from a fresh read, never queue two toggle batches, and reject a stale one at its nonce.
- **Keep other CRV out of the multisig.** `work` pulls any CRV the multisig holds, not just gauge rewards.
- **Independent claims.** Turn off any other routine that claims or sells this gauge's CRV, and review open CRV approvals that are no longer needed.
- **Retain mode.** Withdraw retained CRV to an address other than the multisig. Otherwise the next sale pulls and splits it again.

## Ending the programme

`work` reverts while the escrow is paused, so the final sale comes first:
1. Stop the scheduled keeper and clear pending transactions.
2. Run the final sale, supervised, while the escrow is unpaused. If it is already paused, unpause only for this sale.
3. Verify balances and allocations.
4. Pause the escrow.
5. Withdraw retained CRV.
6. Disable every keeper.
7. Vyper revokes the CRV allowance and mint permission.
