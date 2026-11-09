# Vyper funding

Two Vyper contracts on Ethereum:

- **FundingEscrow** holds yvUSDC-1 and pays exact USDC amounts for future vote weeks. Lifetime spending cannot exceed **100,000 USDC**; yield and recovery never reset the budget.
- **ProceedsEscrow** uses `sell(min_out)` to mint all pending gauge rewards, pull only the freshly minted CRV, sell it through the fixed pool, retain Yearn's share as **crvUSD**, and return the rest atomically. The contract selects the **transaction's UTC week**, not the original earning week.

The manager is `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. It manages keepers, pauses sales and can recover any ERC20 to itself. Public `checkpoint()` freezes the current funded week's vote share and remains usable while paused. Sales checkpoint automatically. There is no emissions reconstruction or unsold inventory.

The [dashboard](dashboard/demo/index.html) has two balances and one table of settlement by claim week. Its [preview](dashboard/preview.png) uses illustrative data. See [accounting](docs/accounting.md) and [verification](docs/verification.md).

## Operating agreement

Vyper delegates this gauge's claims and CRV sales to the automation and stops independent claims, sales and other CRV transactions during the programme. Other approved claimers must also stop. Existing CRV in the multisig is left untouched.

Start automation before the first funded week and clear historical pending rewards near its start. During unfunded weeks, before the programme and after its end, the whole swap output returns to Vyper. Funded weeks retain the frozen Yearn vote share. Both sides accept small amounts crossing rollover being split using the new week's share.

Run frequently and make a best-effort **claim and sale in the last five minutes before Thursday 00:00 UTC**. A checkpoint alone does not clear pending rewards. After a substantial outage or an independent claim, stop normal automation and reconcile with Vyper before restarting. The contract does not detect stale claims, reconstruct missed weeks or recover rewards another caller already minted. Those balances remain with Vyper.

## Build and test

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest
```

Build updates `abi/contracts.json` and the artifacts in `build/`. Commit generated artifacts with contract changes. Keeper/report runs load them without importing the compiler. Fork tests use `ETHEREUM_RPC_URL` and send no live transactions.

## Deploy and fund

Copy `config/example.json` to `config/live.json`. Set the agreed yCRV OTC **payee**, a future Thursday 00:00 UTC **start**, the duration (1–52 weeks) and a dedicated **keeper**. Mainnet dependencies are constants. Constructors take only `FundingEscrow(payee, start, weeks)` and `ProceedsEscrow(funding)`.

```sh
uv run vyper-grant deploy funding --deployer YOUR_ADDRESS
# Submit with your signer; record funding and deployment_block in config.
uv run vyper-grant deploy proceeds --deployer YOUR_ADDRESS
# Record proceeds, then verify:
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Deployments and manager operations prepare unsigned transactions or Safe batches. Vyper grants the CRV allowance and mint permission; Yearn enables the keeper. There is no activation transaction or historical accrual baseline. Run `doctor` at setup; ordinary keeper/report passes do not repeat full deployment verification. Ensure the pre-start closing sale succeeds before entering the first funded week.

Transfer yvUSDC-1 shares to FundingEscrow. The vault is `0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204`; `previewWithdraw(100000000000)` gives the shares for 100,000 USDC at the current price. Recheck at funding time. Raw USDC is recoverable but is not automatically invested or spent. Withdrawals permit zero loss.

## Weekly operation

```sh
uv run vyper-grant purchase --week THURSDAY_TIMESTAMP --usdc 2000 --reference 'OTC agreement'
uv run vyper-grant checkpoint          # simulate a public vote snapshot
uv run vyper-grant checkpoint --send
uv run vyper-grant quote               # simulate claimable(), then call quote(amount)
uv run vyper-grant keeper              # simulate the next action
uv run vyper-grant keeper --send
uv run vyper-grant --out dashboard/dist report
```

Purchases assume all of the designated Yearn voter's gauge weight for the week was bought. They do not cast votes or verify the commercial price. Call `checkpoint()` early in each funded week and before replacing Yearn's vote, preferably in the voting Safe's atomic batch. A missing snapshot is also captured by the first sale. If history was overwritten first, `set-missing-weight --week TIMESTAMP` prepares an archival lookup and manager batch for an unset snapshot.

`claimable()` checkpoints the gauge and returns pending CRV without minting; use **eth_call** to simulate it. `quote(amount)` is a view returning the expected crvUSD output for a supplied CRV amount. The tooling reads both at the same block. The bot discounts the quote by `slippage_bps` and calls `sell(min_out)`. The executed claim may grow between quote and inclusion. There is no amount, week or deadline argument, no oracle floor and no partial sale.

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. `ops/run.sh checkpoint` requests only a snapshot. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` for the dedicated signer. Nothing is scheduled by this repository. Configure the normal minimum trade, slippage and gas limit. Closing sales bypass the minimum in each week's final five minutes, including the pre-start cleanup. A final cleanup also bypasses it in the first five minutes after programme end; that output belongs entirely to Vyper. All sales still require a positive quoted minimum and observe the gas limit. Stop automation and hand claims back to Vyper after the final cleanup.

Use one host/signer and a shared output directory for the process lock. A pending nonce prevents another submission. After a response is lost, the next pass reads fresh claimable rewards; there is no signed-transaction journal or automatic fee replacement. Reports use finalized blocks and intentionally lag execution.

`set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, and `recover funding|proceeds --token ADDRESS --amount-raw INTEGER` cover management. Grant conditions and release of retained proceeds remain the manager's responsibility.

No live deployment is recorded in this checkout. The changed ABI and bytecode require fresh deployments.
