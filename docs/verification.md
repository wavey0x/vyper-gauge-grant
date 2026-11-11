# Verification

Verified November 11, 2026: **53 tests passed**, including an Ethereum fork at block **26,379,075**. No live transaction was sent.

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest -q -s
uv build
```

The fork funds with real USDC and yvUSDC-1, withdraws exact USDC payments and impersonates the fixed Yearn voter to reassign an existing vote in the real controller. It clears a pre-start claim, performs automatic share capture and full-claim sales through the deployed gauge/minter and both fixed pools (CRV → crvUSD → USDC), checks the rollover tail and captures before a revote. It verifies that the full minted amount was sold, existing multisig CRV remains untouched, and actual USDC output is conserved between both parties. It changes the Yearn receiver, verifies forwarding, resumes accumulation, and exercises governance's direct recovery of real vault shares. Post-programme proceeds return entirely to Vyper.

A separate local fork branch raises the gauge weight using impersonated governance until the actual gauge reaches its cap. The final claim is still sold even when the gauge reports killed. The escrow has no local cap accounting. No governance action leaves the fork.

Contract tests cover the unchanged 100k funding budget, liquid USDC spending before vault withdrawal, vault yield/recovery, partial withdrawal rollback, whole-claim settlement, existing CRV/crvUSD/USDC balances, zero/partial/full Yearn shares, rounding, public idempotent checkpointing at the boundary and while paused, bounded missing-weight recovery, and sale permissions. A stale week is rejected before minting, and a claim above `max_crv` rolls back the mint and snapshot. The operator's USDC minimum scales with the actual minted CRV, so permitted claim growth cannot dilute the minimum per CRV; the final minimum rolls back both hops if execution price worsens. Failures of mint permission, allowance, forwarding and return also roll back the snapshot and claim. Receiver changes preserve existing balances and can resume accumulation. Governance recovers shares and raw USDC before, during and after the programme, with sales paused and vault redemption blocked; unauthorized callers cannot recover funding. Tests retain coverage of rollover tails, zero-share settlement outside funded weeks, gauge rate changes/caps, and independent claims.

Operations tests deploy both production contracts through the Python builder with mocks at the fixed mainnet addresses. They cover deployment verification/setup, historical voter reads, a normal one-transaction sale with the encoded week, CRV quote and cap, checkpoint retries while paused, a closing sale below the normal minimum, the post-end cleanup, reporting unfunded sales, manager withdrawals, pending nonces and a lost RPC response after a successful sale. The next pass reads fresh pending rewards instead of repeating the settled claim. Reports distinguish USDC held, Yearn allocation, forwarded amounts and withdrawals. CLI tests execute the prepared manager receiver-change batch and ychad.eth recovery batch; funding recovery works with no proceeds address configured.

Both contracts compile with Vyper 0.4.3 and generated ABI/bytecode matches the sources. Python static checks, shell syntax and whitespace checks pass. The built wheel includes the new ABI and dashboard template. Desktop and 390px mobile layouts were inspected; the dashboard has no horizontal overflow or JavaScript errors.

Cooldown tests verify the 600-second default and hard floor, manager permissions, a shared interval across keepers, the exact eligibility boundary, and `canWork()` while paused. Failed sales leave `last_sale` unchanged. A public checkpoint can capture the new week while the cooldown is active. Operations tests verify that the keeper waits, that the final cleanup still bypasses the trade minimum after the cooldown, and that the CLI prepares a working manager cooldown update.

FundingEscrow is **77 lines** with a **2,721-byte** runtime. Direct USDC support added no mutable state or seller dependency. ProceedsEscrow is **214 lines**, down from the earlier 275-line design, with a **7,099-byte** runtime versus 9,077. Its mutable state is weekly captured/share records, keeper permissions, the pause flag, Yearn's receiver, the cooldown and the last successful sale timestamp. No accrual baselines, inflation rates, caps, inventories, cursors or lifetime accounting totals remain. A funded share is captured once; ordinary sales write only the cooldown timestamp in escrow storage.

These checks verify claim-week settlement, not exact earning-week attribution or compliance with the operating agreement. Closing sales are best effort, external claimers must stop, and substantial outages require reconciliation. `claimable()` uses eth_call to simulate the gauge checkpoint; `quote(amount)` is a separate view. Tests verify that quoting leaves the gauge untouched and claimable checkpoints without minting. No live deployment is recorded; fresh deployment is required. See [accounting](accounting.md) for the accepted assumptions.
