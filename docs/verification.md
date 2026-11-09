# Verification

Verified November 9, 2026: **39 tests passed**, including a real Ethereum fork at block **26,379,075**. No live transaction was sent.

```sh
uv sync --extra dev
RUN_FORK_TESTS=1 uv run --no-sync pytest -q -s
uv run --no-sync vyper-grant --out build build
uv build
```

The fork reads `ETHEREUM_RPC_URL`. It deposits into the real yvUSDC-1 vault, withdraws exact USDC payments, creates a controlled voter's veCRV lock, votes in the real GaugeController, claims from the deployed Vyper gauge, and swaps through the configured CRV/crvUSD pool. It checks the atomic split, preserves a snapshot across a revote, and advances through a 52-week programme in bounded catch-ups. The largest tested 16-week checkpoint used **13,642,310 gas**. This is measured coverage of that fork state, not a universal gas guarantee.

Contract tests cover the lifetime budget despite vault yield and withdrawals, future-week payment restrictions, exact payment rollback, permission changes, immutable weekly ratios, missing-history recovery, annual rate cuts, cap exhaustion, unrelated balances, independent minting, repeated partial sales, dust, and atomic rollback when a swap or return transfer fails. A deterministic 3,000-case test checks the conservative accrual bounds against arbitrary third-party checkpoint partitions. Excessive downtime leaves the accounting cursor unchanged and preserves manager recovery.

Operations tests deploy both contracts in a local EVM and exercise the Python tooling: bytecode and immutable verification, setup authorization, archival reads immediately before boundary-block votes, detection of false historical recovery, keeper simulation and signed execution, journal recovery after an uncertain broadcast, nonce reuse prevention, final small sales, event reconciliation, and reporting after manager withdrawals. Report tests cover exact decimal conversion and script-safe embedding.

Both Vyper contracts compile with 0.4.3. The distributable wheel was extracted outside the checkout and checked for contract compilation, dependency ABIs and dashboard generation. Python static checks, shell syntax and whitespace checks pass. The dashboard was inspected at desktop and 390-pixel mobile widths, with no horizontal overflow or browser errors.

The fork uses a controlled voter to exercise voting semantics; it does not impersonate Yearn governance to approve a real commercial purchase. Historical voter recovery remains a trusted manager action, and the pool's price guard uses its own moving average. These boundaries and the operating stops are described in [accounting](accounting.md). Deployment addresses, the agreed OTC payee, start date and keeper remain unset in the example configuration.
