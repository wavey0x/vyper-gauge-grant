# Vyper funding

Two Vyper contracts on Ethereum, with one normal sale transaction.

- **FundingEscrow** holds yvUSDC-1 and pays exact USDC amounts to a fixed OTC recipient for future emission weeks. Lifetime spending cannot exceed **100,000 USDC**; yield and recovery never reset the budget.
- **ProceedsEscrow** automatically syncs earning-week accounting inside `sell(amount, min_out)`. It sells through the fixed CRV/crvUSD pool, retains Yearn's share as **crvUSD**, and returns Vyper's share atomically. A sale can consume CRV from several weeks, each with its own frozen ratio.

The manager is `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. It controls keeper permissions, pauses sales, and can recover any ERC20 to itself. Public `sync()` remains usable while paused. The operator sets trade size, cadence and slippage; the pool enforces `min_out`. There is no additional oracle or onchain trade policy.

The dashboard has two balances and one weekly table: [illustrative report](dashboard/demo/index.html), [preview](dashboard/preview.png). See [accounting](docs/accounting.md) and [verification](docs/verification.md).

## Build and test

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest
```

The build updates `abi/contracts.json` and unsigned deployment artifacts in `build/`. Commit the generated artifacts with contract changes; tests detect stale artifacts. Keeper/report runs load these files without importing the compiler. The mainnet fork uses `ETHEREUM_RPC_URL` and never sends live transactions.

## Deploy and fund

Copy `config/example.json` to `config/live.json`. Set the agreed OTC **payee**, a future Thursday 00:00 UTC **start** timestamp, the programme duration (1–52 weeks), and a dedicated keeper address. The payee must be the agreed yCRV OTC recipient. Token, vault, gauge, voter, pool and manager addresses are fixed in the tooling.

```sh
uv run vyper-grant deploy funding --deployer YOUR_ADDRESS
# Submit through your signer; record funding and deployment_block in the config.
uv run vyper-grant deploy proceeds --deployer YOUR_ADDRESS
# Record proceeds, then verify the deployments:
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Deployments and manager operations prepare unsigned transactions or Safe batches. Vyper executes its CRV allowance and conditional mint authorization; Yearn enables the keeper. There is no activation transaction: deployment captures the gauge baseline and excludes older earnings. Run `doctor` when configuring or changing deployments; routine keeper/report passes do not repeat full bytecode verification.

Transfer yvUSDC-1 shares to FundingEscrow. The vault is `0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204`; `previewWithdraw(100000000000)` gives the shares needed for 100,000 USDC at the current price. Recheck at funding time. Raw USDC is recoverable but not automatically invested or spent. Vault withdrawals permit zero loss, so unavailable liquidity or a loss stops payment.

## Weekly purchases and syncing

```sh
uv run vyper-grant purchase --week THURSDAY_TIMESTAMP --usdc 2000 --reference 'OTC agreement reference'
uv run vyper-grant sync          # simulate and print call data
uv run vyper-grant sync --send   # permissionless call, signed by the configured operator
```

A purchase assumes all of the designated Yearn voter's gauge weight for that week was bought. It does not cast a vote or verify the commercial price. Grant conditions and the decision to release proceeds remain the manager's responsibility.

Call `sync()` immediately before replacing a gauge vote, preferably in the voting Safe's same atomic batch. It captures the current funded week's ratio even exactly at rollover with no earnings yet. Also plan sync calls around **Wednesday 23:55 UTC and Thursday 00:05 UTC**. The keeper checks chain state and retries a due snapshot on subsequent runs, even if sales are paused or the balance is below the trading minimum. A successful sale also performs the required sync.

## Keeper and dashboard

```sh
uv run vyper-grant keeper        # simulate one action
uv run vyper-grant keeper --send # uses KEEPER_PRIVATE_KEY
uv run vyper-grant --out dashboard/dist report
```

A normal pass simulates `sync()` with `eth_call`, selects an amount, quotes it, applies the configured slippage discount and submits one `sell()`. Boundary/final syncs are submitted separately when no sale can perform them. Final programme remainders may sell below the normal minimum; amounts too small for a positive quoted minimum are left unsold.

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. `ops/run.sh sync` explicitly requests a sync instead of trading. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` only for the dedicated signer. Nothing is scheduled by this repository. Choose the polling cadence, minimum/maximum trade sizes and slippage in the operator's configuration. Reports use finalized blocks and intentionally lag execution.

Use one host and signer, with a shared output directory for its process lock. A pending nonce prevents another submission. After a timeout the next pass reads chain state again; there is no signed-transaction journal or automatic fee replacement. Resolve stuck transactions through the operator's signer. Keep sync intervals below 16 weeks; excessive downtime or unsupported gauge changes stop automation and require operator intervention. Manager token recovery remains available.

`quote`, `set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, and `recover funding|proceeds --token ADDRESS --amount-raw INTEGER` cover the other operations. `set-missing-weight --week TIMESTAMP` prepares an archival lookup and a manager Safe batch for an unset vote snapshot. The contract checks the historical gauge denominator; the supplied Yearn weight remains a trusted manager assertion, with no evidence hash or proof machinery.

There is no live deployment recorded in this checkout. The refactor changes both constructor bytecode and ABIs and requires fresh deployments; it is not an upgrade to an existing escrow.
