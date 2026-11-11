# Vyper funding

Two independent Vyper contracts on Ethereum:

- **FundingEscrow** holds yvUSDC-1. `draw(amount)` withdraws the requested USDC directly to its configurable recipient. There are no programme dates, weekly records or lifetime spending cap; available vault shares fund each draw.
- **ProceedsEscrow** uses `work(min_out, max_crv)` to mint pending gauge rewards, pull up to `max_crv` from the Vyper multisig, sell through the fixed CRV → **crvUSD** route, allocate Yearn's share, and return the rest atomically. Yearn's share accumulates here by default; management can set a receiver for future sales. The contract calculates the **transaction's UTC week** automatically; it does not reconstruct the original earning week.

The manager is `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. It manages keepers, the Yearn proceeds receiver, pauses sales and can withdraw any ERC20 held by ProceedsEscrow to a chosen recipient. **ychad.eth** (`0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`) can independently claw back any ERC20 held by FundingEscrow to itself, and withdraw any ERC20 held by ProceedsEscrow to a chosen recipient, at any time. Neither contract calls or knows the other. Public `checkpoint()` freezes the current week's vote share and remains usable while paused. Sales checkpoint automatically. There is no emissions reconstruction or unsold inventory.

The [dashboard](dashboard/demo/index.html) has two balances, weekly proceeds and an expandable list of dated funding payments. Its [preview](dashboard/preview.png) uses illustrative data. See [accounting](docs/accounting.md) and [verification](docs/verification.md).

## Permissions

| Function | Who can call |
| --- | --- |
| Funding `draw`, `set_recipient` | Yearn manager |
| Proceeds `set_missing_weight`, `set_yearn_receiver`, `set_keeper`, `set_cooldown`, `set_paused` | Yearn manager |
| Funding `recover`, proceeds `withdraw` | Yearn manager or ychad.eth |
| Proceeds `work` | Enabled keepers only, including management if explicitly enabled |
| `checkpoint`, `claimable`, and all views | Anyone |

Public helpers cannot mint, transfer, choose recipients or grant roles. `checkpoint` records only the current week's share derived from the fixed controller and voter; `claimable` checkpoints the fixed gauge for its receiver. Governance withdrawal access does not grant configuration authority. Both governance addresses are pinned to `0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`, rather than following future ENS changes.

## Operating agreement

Vyper delegates this gauge's claims and CRV sales to the automation and stops independent claims, sales and other CRV transactions during the programme. Other approved claimers must also stop. The multisig's CRV balance is dedicated to this workflow, including rewards already minted and leftovers from earlier batches. CRV held directly by the escrow is not included in sales.

All of the designated Yearn voter's weight on this gauge belongs to the programme while this automation operates. Funding can be drawn fortnightly or on another agreed schedule; missing or delayed payments and funding recovery do not disable Yearn's allocation. Ending the arrangement early requires operational coordination and pausing sales while reconciling.

There is no configured start or end date. Deployment captures the current week's share when its vote history is still available. If Yearn has no effective votes this week, `work()` sends the entire output to Vyper, even if Yearn has since voted for next week. Sell historical pending rewards before the first boundary with a nonzero Yearn share. After that boundary, sales use the new week's share; there is no carryover split. Both sides accept small amounts crossing rollover under this convention. Stop or pause automation and coordinate the final settlement when the programme ends.

Run frequently and make a best-effort **claim and sale in the last five minutes before Thursday 00:00 UTC**, allowing for the sale cooldown. A checkpoint alone does not clear pending rewards. After a substantial outage or an independent claim, stop normal automation and reconcile with Vyper before restarting. The contract does not detect stale rewards or reconstruct missed weeks. Already-minted CRV remaining in the multisig is available for subsequent sales.

## Build and test

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest
```

Build updates `abi/contracts.json` and the artifacts in `build/`. Commit generated artifacts with contract changes. Keeper/report runs load them without importing the compiler. Fork tests use `ETHEREUM_RPC_URL` and send no live transactions.

## Deploy and fund

Both constructors take **no arguments**: `FundingEscrow()` and `ProceedsEscrow()`. Mainnet dependencies and management addresses are constants. The funding recipient defaults to fee burner `0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8`; management can change it with `set-recipient --account ADDRESS`.

To deploy both contracts using the encrypted Brownie account `wavey3` and verify their source on Etherscan:

```sh
uv run --extra dev python -m vyper_grant.deploy --account wavey3 --broadcast
```

The script prompts locally for the keystore password and uses `ETHEREUM_RPC_URL` (or `MAINNET_RPC_URL`) and `ETHERSCAN_KEY`. It compiles the exact Vyper standard JSON submitted to Etherscan and checks that it matches the committed bytecode before signing. It uses the account's existing Brownie keystore with the project's signer; Brownie itself is not needed. Without `--broadcast`, it only estimates gas and reads the account's public address.

The script records each transaction hash before broadcasting in `build/deployment.json`, then saves confirmed addresses and the first deployment block in `config/live.json`. Keep the journal: repeating the command resumes recorded deployments instead of deploying duplicates. An unresolved recorded transaction must be investigated before proceeding. If only verification needs retrying, run:

```sh
uv run --extra dev python -m vyper_grant.deploy --verify-only
```

Deployment grants no keeper permission and funds neither contract. Set the dedicated `keeper` in `config/live.json`, then prepare the two setup batches:

```sh
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Vyper grants the CRV allowance and mint permission; Yearn enables the keeper. Run `doctor` again after the batches execute. Deploy before Yearn votes if possible: the constructor can preserve this week's effective vote before the controller overwrites it. If history was already overwritten, deployment still succeeds, but sales need a manager-supplied missing weight for that week. A successful sale before the boundary clears the old rewards at this week's split. Neither contract requires an activation transaction.

The original `vyper-grant deploy funding|proceeds --deployer ADDRESS` commands remain available for unsigned deployments with another signer. All manager commands prepare Safe batches. Funding operations need no proceeds address; proceeds deployment, setup, management and the keeper need no funding address. The combined report reads both.

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

`claimable()` returns the multisig's existing CRV balance plus pending gauge rewards; use **eth_call** to simulate its gauge checkpoint. `quote(amount)` is a view returning expected crvUSD (18 decimals) for a supplied CRV amount (18 decimals), through the fixed TriCRV pool. The tooling reads both at the same block.

The keeper chooses `amount = min(claimable(), max_crv)` using the configured batch limit (default **15,000 CRV**), computes `min_out = quote(amount) * 995 // 1000` with the default 50 bps discount, and calls `work(min_out, amount)`. There is no quote multiplier or growth buffer. The contract claims pending rewards, pulls `min(receiver_balance, max_crv)`, and swaps exactly that amount with `min_out` unchanged. Excess CRV stays in the multisig for a later call. If less is available, the swap still has to meet the same absolute minimum. An empty batch reverts. A transaction included after a week boundary uses the new week's split, even if quoted before that boundary. Failed transfers or swaps roll back the claim, pull and snapshot.

`canWork()` is a view of global readiness: sales must be unpaused and at least `cooldown()` seconds past `last_sale()`. The default and hard minimum are **600 seconds**. It does not check caller permissions, pending rewards, vote history or swap execution; keepers still quote and simulate the full sale. Only a successful sale updates `last_sale`, shared by all keepers. Public checkpoints remain available during the cooldown.

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. The keeper checks `canWork()` before quoting; while waiting it can still capture a missing weekly snapshot. `ops/run.sh checkpoint` requests only a snapshot. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` for the dedicated signer. Nothing is scheduled by this repository. Configure the normal minimum trade, slippage and gas limit. Closing sales bypass the minimum trade threshold in each week's final five minutes, but still respect the batch cap. Drain any substantial backlog ahead of that window. Plan earlier sales so the cooldown expires in that window; a sale too early in the window can prevent another attempt closer to the boundary. All sales still respect the cooldown, require a positive quoted minimum and observe the gas limit. A pending sale may land after the boundary and use the new split; its nonce can block later submissions until it is mined or manually replaced. If the closing sale misses the boundary, reconcile rather than assuming the next week's sale preserves the previous share. Stop automation and hand claims back to Vyper after the final cleanup.

Use one host/signer and a shared output directory for the process lock. A pending nonce prevents another submission. After a response is lost, the next pass reads fresh claimable rewards; there is no signed-transaction journal or automatic fee replacement. Reports use finalized blocks and intentionally lag execution.

`set-recipient --account ADDRESS` changes the recipient of future funding draws. `set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, and `set-cooldown --seconds INTEGER` (at least 600) manage sales. Cooldown changes apply immediately relative to the last successful sale. `set-yearn-receiver --account ADDRESS` prepares a manager batch to route future Yearn shares there. Use the proceeds contract's own address to resume accumulation. Zero is rejected; existing balances stay where they are.

`withdraw --token ADDRESS --amount-raw INTEGER --recipient ADDRESS` prepares a manager batch; add `--governance` to prepare a **ychad.eth** batch. Both call ProceedsEscrow's `withdraw(token, amount, recipient)`. Use it to pay TheDAO directly from accumulated crvUSD or move any other ERC20 held by the proceeds contract. It works while paused, rejects zero and the escrow itself as recipients, and emits `Withdrawn(token, amount, recipient)`. It does not change the receiver of future sales. Grant conditions and release of held proceeds remain management's responsibility.

`recover funding --token ADDRESS --amount-raw INTEGER` prepares a funding recovery to the manager; add `--governance` for a **ychad.eth** clawback to governance. Recover yvUSDC-1 shares directly to avoid relying on vault redemption. Recovery is independent of programme dates, the seller and the vault's withdrawal availability; it does not reverse payments already made. The governance address is pinned to its ENS resolution verified on November 10, 2026, not resolved dynamically by the contract.

Funding amounts remain 6-decimal USDC; proceeds quotes, minimums, balances and allocations are 18-decimal crvUSD. The quote command returns `crvusd`, keeper output uses `min_crvusd`, and reports use `forwarded_crvusd` / `withdrawn_crvusd`. Regenerate reports from the new deployment rather than reusing saved USDC reports.

The earlier four-argument proceeds deployment does not implement this API. These changes require a fresh proceeds deployment and new Vyper approvals for its address. FundingEscrow is unchanged and can be reused. Do not reuse the old deployment journal for changed bytecode.


To replace only proceeds, use a separate config with the existing funding address and original `deployment_block`, but `proceeds: null`. Keep the old journal and select a new journal file:

```sh
uv run --extra dev python -m vyper_grant.deploy --account wavey3 --only proceeds \
  --config output/proceeds-v2.json --journal build/proceeds-v2.json --broadcast
```

After deployment and verification, use the replacement config for setup and operation (or copy it to `config/live.json`). Keep the old seller disabled; obtain Vyper's token and minter approvals for the new address and enable the keeper there. The script never changes either existing onchain contract.
