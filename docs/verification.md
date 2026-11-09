# Verification

Verified November 9, 2026: **41 tests passed**, including a real Ethereum fork at block **26,379,075**. No live transaction was sent.

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest -q -s
uv build
```

The fork deposits into the real yvUSDC-1 vault, withdraws exact USDC payments, creates a controlled veCRV voter, votes in the real GaugeController, claims from the deployed Vyper gauge and sells through the fixed pool. The first sale performs its own sync. Subsequent checks cover capture before a revote and a 52-week programme with bounded catch-ups. The largest tested 16-week sync used **13,629,336 gas**; this measures that fork state and is not a universal gas guarantee.

A separate branch of the same local fork changes gauge weight under an impersonated governance account until the real fundraising gauge reaches its cap. Its `is_killed()` then returns true. The escrow still books and sells the final earnings. This catches the production behavior the previous mock omitted; the mock now reflects it too. No governance or token transaction leaves the fork.

Contract tests exercise the 100k budget, vault yield and recovery, exact withdrawal failure rollback, first-sale auto-sync, permissionless repeated sync, capture at the exact weekly boundary, syncing while paused, missing-weight recovery bounds, independent minting, annual rate changes, cap exhaustion, unrelated balances, and atomic rollback of sync/claim/pull/swap/return. One swap consumes two weeks with different ratios; another consumes a one-wei old remainder alongside newer earnings. A deterministic 3,000-case test checks conservative attribution under arbitrary third-party checkpoint partitions.

Operations tests deploy both contracts in a local EVM and exercise setup, deployment verification, historical reads before a boundary-block revote, the normal one-sale keeper pass, pre/post-boundary retries while paused, final small sales, pending-nonce handling, and recovery after a sale succeeds but its RPC response is lost. Finalized event reports preserve earned totals after manager withdrawals. Tests also check precise decimal conversion, script-safe rendering and that generated artifacts match the Vyper sources.

Both contracts compile with Vyper 0.4.3. Routine keeper and report code loads generated artifacts without importing Vyper. The wheel is checked outside the checkout for artifact loading and dashboard generation with compiler imports disabled. Python static checks, shell syntax and whitespace checks pass. The dashboard retains its previously inspected desktop/mobile layout; this refactor changes its data source, not the interface.

Weekly storage is reduced from seven fields to four: captured flag, Yearn weight, total weight and remaining CRV. There are no stored lifetime reporting totals or sale output counters. The cap is immutable; rate/epoch writes occur only at an annual boundary, and unchanged accrual/inventory writes are skipped. The keeper owns slippage, sizing and cadence; these are intentionally no longer enforced as additional contract policies.

Historical voter recovery remains a trusted manager action. The fork uses a controlled voter, not a real Yearn governance authorization. The agreed payee, programme start and keeper must be configured before fresh deployments. Operating assumptions and stops are described in [accounting](accounting.md).
