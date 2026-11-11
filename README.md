# Vyper funding

Two independent Vyper contracts on Ethereum:

- **FundingEscrow** holds yvUSDC-1. `draw(amount)` withdraws the requested USDC directly to its configurable recipient. There are no programme dates, weekly records or lifetime spending cap; available vault shares fund each draw.
- **ProceedsEscrow** uses `work(min_out, expected_week, quoted_crv, max_crv)` to mint pending gauge rewards, pull only the freshly minted CRV, sell through the fixed CRV → **crvUSD** route, allocate Yearn's share, and return the rest atomically. Yearn's share accumulates here by default; management can set a receiver for future sales. The contract calculates the **transaction's UTC week** and checks it matches `expected_week`; it does not reconstruct the original earning week.

The manager is `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. It manages keepers, the Yearn proceeds receiver, pauses sales and can recover any ERC20 to itself. **ychad.eth** (`0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`) can independently claw back any ERC20 held by FundingEscrow, to itself, at any time. Neither contract calls or knows the other. Public `checkpoint()` freezes the current programme week's vote share and remains usable while paused. Sales checkpoint automatically. There is no emissions reconstruction or unsold inventory.

The [dashboard](dashboard/demo/index.html) has two balances, weekly proceeds and an expandable list of dated funding payments. Its [preview](dashboard/preview.png) uses illustrative data. See [accounting](docs/accounting.md) and [verification](docs/verification.md).

## Operating agreement

Vyper delegates this gauge's claims and CRV sales to the automation and stops independent claims, sales and other CRV transactions during the programme. Other approved claimers must also stop. Existing CRV in the multisig is left untouched.

All of the designated Yearn voter's weight on this gauge belongs to the programme during ProceedsEscrow's active dates. Funding can be drawn fortnightly or on another agreed schedule; missing or delayed payments and funding recovery do not disable Yearn's allocation. Ending the arrangement early requires operational coordination and pausing sales while reconciling.

Start automation before the programme and clear historical pending rewards near its start. Before START and at or after END, the whole swap output returns to Vyper. Every programme week retains its frozen Yearn vote share. Both sides accept small amounts crossing rollover being split using the new week's share.

Run frequently and make a best-effort **claim and sale in the last five minutes before Thursday 00:00 UTC**, allowing for the sale cooldown. A checkpoint alone does not clear pending rewards. After a substantial outage or an independent claim, stop normal automation and reconcile with Vyper before restarting. The contract does not detect stale claims, reconstruct missed weeks or recover rewards another caller already minted. Those balances remain with Vyper.

## Build and test

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest
```

Build updates `abi/contracts.json` and the artifacts in `build/`. Commit generated artifacts with contract changes. Keeper/report runs load them without importing the compiler. Fork tests use `ETHEREUM_RPC_URL` and send no live transactions.

## Deploy and fund

Copy `config/example.json` to `config/live.json`. For proceeds, set a future Thursday 00:00 UTC **start**, the duration (1–52 weeks) and a dedicated **keeper**. Mainnet dependencies are constants. Constructors are `FundingEscrow()` and `ProceedsEscrow(start, weeks)`; either contract can be deployed first. The funding recipient defaults to fee burner `0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8`; management can change it with `set-recipient --account ADDRESS`.

```sh
uv run vyper-grant deploy funding --deployer YOUR_ADDRESS
# Submit with your signer; record funding in config.
uv run vyper-grant deploy proceeds --deployer YOUR_ADDRESS
# Record proceeds and the earliest deployment_block for the combined report, then verify:
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Deployments and manager operations prepare unsigned transactions or Safe batches. Vyper grants the CRV allowance and mint permission; Yearn enables the keeper. Funding operations need no proceeds address or start date; proceeds deployment, setup, management and the keeper need no funding address. The combined dashboard report reads both contracts. There is no activation transaction or historical accrual baseline. Run `doctor` at setup; ordinary keeper/report passes do not repeat full deployment verification. Ensure the pre-start closing sale succeeds before entering the programme.

Transfer yvUSDC-1 shares to FundingEscrow. The vault is `0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204`; `previewWithdraw(amount)` gives the shares needed for a USDC amount at the current price. Each draw calls `withdraw(amount, recipient, self, 0)`, letting the vault calculate and burn the required shares with zero permitted loss. `assets()` reports the USDC value of the remaining shares, including yield. Loose USDC is not used by draws or included in that value; it can be recovered through management.

## Payments and weekly operation

```sh
uv run vyper-grant draw --usdc 2000    # when payment is due, e.g. fortnightly
uv run vyper-grant checkpoint          # simulate a public vote snapshot
uv run vyper-grant checkpoint --send
uv run vyper-grant quote               # simulate claimable(), then call quote(amount)
uv run vyper-grant keeper              # simulate the next action
uv run vyper-grant keeper --send
uv run vyper-grant --out dashboard/dist report
```

`draw(amount)` takes only the USDC amount (6 decimals on chain). It pays `recipient()` and emits a payment event. It can be called at any time and does not assign payments to vote weeks. The dashboard records actual payment dates separately from weekly proceeds.

The proceeds contract reads the designated Yearn voter's actual gauge weight every week, independently of payments. A draw does not cast votes or verify their commercial price. Call `checkpoint()` early in each programme week and before replacing Yearn's vote, preferably in the voting Safe's atomic batch. A missing snapshot is also captured by the first sale. If history was overwritten first, `set-missing-weight --week TIMESTAMP` prepares an archival lookup and manager batch for an unset snapshot.

`claimable()` checkpoints the gauge and returns pending CRV without minting; use **eth_call** to simulate it. `quote(amount)` is a view returning expected crvUSD (18 decimals) for a supplied CRV amount (18 decimals), through the fixed TriCRV pool. The tooling reads both at the same block. The bot discounts the quote by `slippage_bps` and passes that minimum, the current week, quoted CRV amount and maximum CRV amount to `work`. The maximum is four times the quoted claim plus 1 CRV of rounding room. The contract scales the minimum to the amount actually minted, preserving the operator's minimum output **per CRV** if the claim grows before inclusion. A transaction mined in a later week or with a larger claim reverts. It still sells the full claim, with no deadline, oracle floor or partial sale. A failed check rolls back the swap and the claim.

`canWork()` is a view of global readiness: sales must be unpaused and at least `cooldown()` seconds past `last_sale()`. The default and hard minimum are **600 seconds**. It does not check caller permissions, pending rewards, vote history or swap execution; keepers still quote and simulate the full sale. Only a successful sale updates `last_sale`, shared by all keepers. Public checkpoints remain available during the cooldown.

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. The keeper checks `canWork()` before quoting; while waiting it can still capture a missing weekly snapshot. `ops/run.sh checkpoint` requests only a snapshot. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` for the dedicated signer. Nothing is scheduled by this repository. Configure the normal minimum trade, slippage and gas limit. Closing sales bypass the minimum in each week's final five minutes, including the pre-start cleanup. A final cleanup also bypasses it until one configured cooldown plus five minutes after programme end; that output belongs entirely to Vyper. All sales still respect the cooldown, require a positive quoted minimum and observe the gas limit. A stale sale reverts; its pending nonce may still block the bot until mined or manually replaced. If the closing sale misses the boundary, reconcile rather than assuming the next week's sale preserves the previous share. Stop automation and hand claims back to Vyper after the final cleanup.

Use one host/signer and a shared output directory for the process lock. A pending nonce prevents another submission. After a response is lost, the next pass reads fresh claimable rewards; there is no signed-transaction journal or automatic fee replacement. Reports use finalized blocks and intentionally lag execution.

`set-recipient --account ADDRESS` changes the recipient of future funding draws. `set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, `set-cooldown --seconds INTEGER` (at least 600), and `recover funding|proceeds --token ADDRESS --amount-raw INTEGER` cover other management. Cooldown changes apply immediately relative to the last successful sale. `set-yearn-receiver --account ADDRESS` prepares a manager batch to route future Yearn shares there. Use the proceeds contract's own address to resume accumulation. Zero is rejected; existing balances stay where they are and can be recovered separately. Grant conditions and release of held proceeds remain the manager's responsibility.

`recover funding --governance --token ADDRESS --amount-raw INTEGER` prepares a **ychad.eth** clawback batch. Recover yvUSDC-1 shares directly to avoid relying on vault redemption. Recovery is independent of programme dates, the seller and the vault's withdrawal availability; it does not reverse payments already made. The governance address is pinned to its ENS resolution verified on November 10, 2026, not resolved dynamically by the contract.

Funding amounts remain 6-decimal USDC; proceeds quotes, minimums, balances and allocations are 18-decimal crvUSD. The quote command returns `crvusd`, keeper output uses `min_crvusd`, and reports use `forwarded_crvusd` / `withdrawn_crvusd`. Regenerate reports from the new deployment rather than reusing saved USDC reports.

No live deployment is recorded in this checkout. The changed proceeds ABI and bytecode require a fresh proceeds deployment; FundingEscrow is unchanged.
