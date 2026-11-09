# Verification

Verified November 9, 2026: **39 tests passed**, including an Ethereum fork at block **26,379,075**. No live transaction was sent.

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest -q -s
uv build
```

The fork deposits into real yvUSDC-1, withdraws exact USDC payments and impersonates the fixed Yearn voter to reassign an existing vote in the real controller. It clears a pre-start claim, performs automatic share capture and full-claim sales through the deployed gauge/minter/pool, checks the rollover tail and captures before a revote. It verifies that the full minted amount was sold, existing multisig CRV remains untouched, and actual crvUSD output is conserved between both parties. Post-programme proceeds return entirely to Vyper.

A separate local fork branch raises the gauge weight using impersonated governance until the actual gauge reaches its cap. The final claim is still sold even when the gauge reports killed. The escrow has no local cap accounting. No governance action leaves the fork.

Contract tests cover the unchanged 100k funding budget, vault yield/recovery, withdrawal rollback, whole-claim settlement, existing CRV and crvUSD balances, zero/partial/full Yearn shares, rounding, public idempotent checkpointing at the boundary and while paused, bounded missing-weight recovery, and sale permissions. Failures of mint permission, allowance, slippage and return transfer roll back the snapshot and claim. Tests exercise the new week's treatment of a small rollover tail, zero-share settlement outside funded weeks, gauge rate changes and cap exhaustion, and the deliberate exclusion of rewards another caller already minted. The deleted emissions estimator and its reference-model tests are no longer part of this design.

Operations tests deploy both production contracts through the Python builder with mocks at the fixed mainnet addresses. They cover deployment verification/setup, historical voter reads, a normal one-transaction sale, checkpoint retries while paused, a closing sale below the normal minimum, the post-end cleanup, reporting unfunded sales, manager withdrawals, pending nonces and a lost RPC response after a successful sale. The next pass reads fresh pending rewards instead of repeating the settled claim.

Both contracts compile with Vyper 0.4.3 and generated ABI/bytecode matches the sources. Python static checks, shell syntax and whitespace checks pass. The built wheel loads the new ABI and renders the dashboard outside the checkout with compiler imports blocked. Desktop and 390px mobile layouts were inspected; the dashboard has no horizontal overflow or JavaScript errors.

ProceedsEscrow is **160 lines**, down from 275, and its compiled runtime is **5,265 bytes**, down from 9,077. Its only mutable state is weekly captured/share records, keeper permissions and the pause flag. No accrual baselines, inflation rates, caps, inventories, cursors or lifetime accounting totals remain. A funded share is captured once; ordinary sales need no persistent escrow accounting writes.

These checks verify claim-week settlement, not exact earning-week attribution or compliance with the operating agreement. Closing sales are best effort, external claimers must stop, and substantial outages require reconciliation. `quote()` uses eth_call to simulate the gauge checkpoint. No live deployment is recorded; fresh deployment is required. See [accounting](accounting.md) for the accepted assumptions.
