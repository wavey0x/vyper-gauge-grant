# Vyper funding

Two independent Vyper contracts on Ethereum:

- **FundingEscrow** holds yvUSDC-1. `fund_round(amount)` queues two equal USDC payments at the next two Thursday 00:00 UTC boundaries. Public `work()` releases one due payment directly from the vault to the configurable recipient. Each round ends after two payments.
- **ProceedsEscrow** uses `work(min_out, max_crv, sell_vyper)` to mint pending gauge rewards and pull up to `max_crv` from the Vyper multisig. With `sell_vyper=True`, it sells the batch through the fixed CRV → crvUSD → **USDC** route and returns Vyper's share atomically. With `False`, it sells only Yearn's CRV share and holds Vyper's CRV for withdrawal by Vyper. Yearn's USDC accumulates here by default; management can set a receiver for future sales. The contract calculates the **transaction's UTC week** automatically; it does not reconstruct the original earning week.

The manager is `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. It manages keepers, the Yearn proceeds receiver, pauses sales and can withdraw any ERC20 except CRV held by ProceedsEscrow to a chosen recipient. **ychad.eth** (`0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`) can independently claw back any ERC20 held by FundingEscrow to itself, and withdraw any ERC20 except CRV held by ProceedsEscrow to a chosen recipient, at any time. Only the Vyper multisig can withdraw proceeds escrow CRV. Neither contract calls or knows the other. Public `checkpoint()` freezes the current week's vote share and remains usable while paused. Sales checkpoint automatically. There is no emissions reconstruction or reserve counter.

The [dashboard](dashboard/demo/index.html) has the funding, proceeds and Vyper CRV balances, weekly proceeds, a transaction list and both escrow addresses. Its [preview](dashboard/preview.png) uses illustrative data. See [accounting](docs/accounting.md) and [verification](docs/verification.md).

## Ethereum mainnet deployment

The current contracts were deployed on November 19, 2026. Both have verified Vyper 0.4.3 source on Etherscan.

| Contract | Address | Deployment transaction | Block |
| --- | --- | --- | ---: |
| FundingEscrow | [0x3602642D28fA686ff74d4C85aF5B6fBe8722CCD8](https://etherscan.io/address/0x3602642D28fA686ff74d4C85aF5B6fBe8722CCD8#code) | [0x43040239602b2f8f9477f34f2bf4a2036c21850683a90a07ebf6f57630fe9658](https://etherscan.io/tx/0x43040239602b2f8f9477f34f2bf4a2036c21850683a90a07ebf6f57630fe9658) | 26451282 |
| ProceedsEscrow | [0x38cf14DaC1329ff404739379004909c6847ab3aC](https://etherscan.io/address/0x38cf14DaC1329ff404739379004909c6847ab3aC#code) | [0x93060472fe1b36cebe85c954b2f0d180fc4bbfc28e58cc9cc99af3bf5d0e840c](https://etherscan.io/tx/0x93060472fe1b36cebe85c954b2f0d180fc4bbfc28e58cc9cc99af3bf5d0e840c) | 26451283 |

The tracked [mainnet config](config/mainnet.json) contains these addresses and the first deployment block. Copy it to the ignored local config before running operational commands:

```sh
cp config/mainnet.json config/live.json
```

Keeper authorization, Vyper's approvals and funding are separate setup steps; the tracked config leaves `keeper` unset.

## Permissions

| Function | Who can call |
| --- | --- |
| Funding `fund_round`, `set_recipient` | Yearn manager |
| Funding `cancel`, `recover` | Yearn manager or ychad.eth |
| Funding `work` | Anyone, only when a scheduled payment is due |
| Proceeds `set_missing_weight`, `set_yearn_receiver`, `set_keeper`, `set_cooldown`, `set_paused` | Yearn manager |
| Proceeds `withdraw` for tokens other than CRV | Yearn manager or ychad.eth |
| Proceeds `withdraw` for CRV | Vyper multisig only |
| Proceeds `work` | Enabled keepers only, including management if explicitly enabled |
| `checkpoint`, `claimable`, and all views | Anyone |

Public proceeds helpers cannot mint, transfer, choose recipients or grant roles. Public funding `work()` can only execute a due payment already authorized by the manager; the caller chooses neither its amount nor recipient. `checkpoint` records only the current week's share derived from the fixed controller and voter; `claimable` checkpoints the fixed gauge for its receiver. Governance withdrawal access does not grant configuration authority. Both governance addresses are pinned to `0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52`, rather than following future ENS changes.

## Operating agreement

Vyper delegates this gauge's claims and CRV sales to the automation and stops independent claims, sales and other CRV transactions during the programme. Other approved claimers must also stop. The multisig's CRV balance is dedicated to this workflow, including rewards already minted and leftovers from earlier batches. All CRV held directly by the escrow belongs to Vyper, including direct transfers, and is excluded from future sales in either mode. Withdraw retained CRV to a separate address during operation. Sending it back to the gauge-receiving multisig makes it eligible for another pull and allocation; revoke the allowance or finish the programme before doing so.

All of the designated Yearn voter's weight on this gauge belongs to the programme while this automation operates. Funding rounds are authorized when voting, with payment at the start of each of the following two weeks; missing or delayed payments and funding recovery do not disable Yearn's allocation. Ending the arrangement early requires operational coordination and pausing sales while reconciling.

There is no configured start or end date. Deployment captures the current week's share when its vote history is still available. If Yearn has no effective votes this week, the entire batch belongs to Vyper: `work()` either sends it USDC or retains its CRV, depending on the mode, even if Yearn has since voted for next week. Process historical pending rewards before the first boundary with a nonzero Yearn share. After that boundary, calls use the new week's share; there is no carryover split. Both sides accept small amounts crossing rollover under this convention. Stop or pause automation and coordinate the final settlement when the programme ends.

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

For a future fresh deployment of both contracts using the encrypted Brownie account `wavey3`, use new config and journal paths. The script verifies their source on Etherscan before the new config becomes active locally:

```sh
uv run --extra dev python -m vyper_grant.deploy --account wavey3 \
  --config output/new-live.json --journal build/new-deployment.json --broadcast \
  && cp output/new-live.json config/live.json
```

The script prompts locally for the keystore password and uses `ETHEREUM_RPC_URL` (or `MAINNET_RPC_URL`) and `ETHERSCAN_KEY`. It compiles the exact Vyper standard JSON submitted to Etherscan and checks that it matches the committed bytecode before signing. It uses the account's existing Brownie keystore with the project's signer; Brownie itself is not needed. Without `--broadcast`, it only estimates gas and reads the account's public address.

The script records each transaction hash before broadcasting in the selected journal, then saves confirmed addresses and the first deployment block in the selected config. Keep the journal: repeating the command resumes recorded deployments instead of deploying duplicates. An unresolved recorded transaction must be investigated before proceeding. If only verification needs retrying, run:

```sh
uv run --extra dev python -m vyper_grant.deploy \
  --config output/new-live.json --journal build/new-deployment.json --verify-only \
  && cp output/new-live.json config/live.json
```

Deployment grants no keeper permission and funds neither contract. Set the dedicated `keeper` in `config/live.json`, then prepare the two setup batches:

```sh
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Vyper grants the CRV allowance and mint permission; Yearn enables the keeper. Run `doctor` again after the batches execute. Deploy before Yearn votes if possible: the constructor can preserve this week's effective vote before the controller overwrites it. If history was already overwritten, deployment still succeeds, but sales need a manager-supplied missing weight for that week. A successful sale before the boundary clears the old rewards at this week's split. Neither contract requires an activation transaction.

The original `vyper-grant deploy funding|proceeds --deployer ADDRESS` commands remain available for unsigned deployments with another signer. All manager commands prepare Safe batches. Funding operations need no proceeds address; proceeds deployment, setup, management and the keeper need no funding address. The combined report reads both.

Transfer yvUSDC-1 shares to FundingEscrow. The vault is `0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204`; `previewWithdraw(amount)` gives the shares needed for a USDC amount at the current price. Each funding payment calls `withdraw(amount, recipient, self, 0)`, letting the vault calculate and burn the required shares with zero permitted loss. `assets()` reports the USDC value of the remaining shares, including yield. Loose USDC is not used for scheduled payments or included in that value; it can be recovered through management.

## Payments and weekly operation

```sh
uv run vyper-grant fund-round --usdc 20000  # queue 10,000 at each of the next two boundaries
uv run vyper-grant funding-keeper          # simulate the next funding payment
uv run vyper-grant funding-keeper --send
uv run vyper-grant checkpoint          # simulate a public vote snapshot
uv run vyper-grant checkpoint --send
uv run vyper-grant quote               # quote the batch using the configured sell_vyper mode
uv run vyper-grant keeper              # simulate the next action
uv run vyper-grant keeper --send
uv run vyper-grant --out dashboard/dist report
```

`fund_round(amount)` takes the total USDC amount (6 decimals on chain) and sends nothing immediately. It queues `amount // 2` for `(block.timestamp // WEEK + 1) * WEEK` and the same amount for one week later. Any rounding dust stays in the vault. These use the same Thursday 00:00 UTC boundaries as proceeds snapshots. Queue the round during the voting week, preferably alongside the vote; a transaction included after the boundary schedules the following two weeks. A new round requires the previous one to be completed or cancelled.

Funding `canWork()` means a payment is pending and its boundary has arrived. Public `work()` has no parameters and releases exactly one payment to `recipient()`. A late call does not shift the second boundary; if both payments are overdue, two calls can settle them. There are no recurring payments, keeper permissions, weekly mappings or links to the proceeds contract. Funds stay invested in yvUSDC-1 until each withdrawal. Insufficient shares or unavailable vault liquidity revert the call and leave the payment pending; the keeper simulates before sending.

`cancel-round` prepares a manager cancellation; add `--governance` for ychad.eth. Cancellation clears only unpaid instalments. Recovering any yvUSDC-1 also cancels the unpaid schedule, so later deposits cannot revive it. The dashboard shows the next payment and records actual paid amounts separately from weekly proceeds.

The proceeds contract reads the designated Yearn voter's actual gauge weight every week, independently of payments. Scheduling or paying a round does not cast votes or verify their commercial price. Call `checkpoint()` early in each programme week and before replacing Yearn's vote, preferably in the voting Safe's atomic batch. A missing snapshot is also captured by the first sale. If history was overwritten first, `set-missing-weight --week TIMESTAMP` prepares an archival lookup and manager batch for an unset snapshot.

`claimable()` returns the multisig's existing CRV balance plus pending gauge rewards; use **eth_call** to simulate its gauge checkpoint. `quote(amount)` is a view returning expected USDC (6 decimals) for a supplied CRV amount (18 decimals), through TriCRV and the fixed crvUSD/USDC pool. The tooling reads both at the same block.

The keeper chooses `amount = min(claimable(), max_crv)` using the configured batch limit (default **15,000 CRV**). With `sell_vyper: true` (the default), it quotes that full amount. Set `sell_vyper: false` to simulate `checkpoint()` at the same block and quote only `amount * share // 10**18`. It applies the default 50 bps discount to that quote and calls `work(min_out, amount, sell_vyper)`. The `quote` command uses the same calculation and returns the pull amount, swap amount and retained amount separately. There is no quote multiplier or growth buffer.

The contract claims pending rewards and pulls `min(receiver_balance, max_crv)`. In retention mode it splits that new batch in CRV, sells only Yearn's portion, and retains the remainder. No existing escrow CRV is sold, even when a later call switches back to `True`. Excess CRV above the pull cap stays in the multisig. The swap receives `min_out` unchanged; if less is available it still has to meet that absolute minimum. When Yearn's share is zero in retention mode, the batch is retained without a swap and `min_out` must be zero. An empty batch reverts. A transaction included after a week boundary uses the new week's split; in retention mode this can change the swap amount too, so refresh quotes after rollover. Failed transfers or swaps roll back the claim, pull and snapshot.

`canWork()` is a view of global readiness: work must be unpaused and at least `cooldown()` seconds past `last_sale()`. The default and hard minimum are **600 seconds**. It does not check caller permissions, pending rewards, vote history or swap execution; keepers still quote and simulate the full call. Every successful `work()` updates `last_sale`, including a batch retained without a swap. Public checkpoints remain available during the cooldown.

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. The keeper checks `canWork()` before quoting; while waiting it can still capture a missing weekly snapshot. `ops/run.sh checkpoint` requests only a snapshot. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` for the dedicated signer. Nothing is scheduled by this repository. Configure the normal minimum trade, slippage and gas limit. Closing sales bypass the minimum trade threshold in each week's final five minutes, but still respect the batch cap. Drain any substantial backlog ahead of that window. Plan earlier sales so the cooldown expires in that window; a sale too early in the window can prevent another attempt closer to the boundary. All calls respect the cooldown and gas limit. Swaps require a positive quoted minimum; retention without a swap uses zero. A pending sale may land after the boundary and use the new split; its nonce can block later submissions until it is mined or manually replaced. If the closing sale misses the boundary, reconcile rather than assuming the next week's sale preserves the previous share. Stop automation and hand claims back to Vyper after the final cleanup.

Run `ops/run.sh funding-keeper` on the same polling schedule for funding payments; it needs no proceeds address or keeper authorization. The default `ops/run.sh` continues to operate proceeds only. Use one host/signer and a shared output directory for the process lock. A pending nonce prevents another submission. After a response is lost, the next pass reads fresh claimable rewards; there is no signed-transaction journal or automatic fee replacement. Reports read the latest mined block.

`set-recipient --account ADDRESS` changes the recipient of all unpaid funding instalments, including the current round. `set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, and `set-cooldown --seconds INTEGER` (at least 600) manage sales. Cooldown changes apply immediately relative to the last successful sale. `set-yearn-receiver --account ADDRESS` prepares a manager batch to route future Yearn shares there. Use the proceeds contract's own address to resume accumulation. Zero is rejected; existing balances stay where they are.

`withdraw --token ADDRESS --amount-raw INTEGER --recipient ADDRESS` prepares a manager batch for non-CRV tokens; add `--governance` to prepare a **ychad.eth** batch. Use it to pay TheDAO directly from accumulated USDC. For CRV, the command automatically prepares a **Vyper multisig** batch and rejects `--governance`: neither Yearn manager nor governance can withdraw CRV. All use `withdraw(token, amount, recipient)`, work while paused, reject zero and the escrow itself as recipients, and emit `Withdrawn(token, amount, recipient)`. Withdrawals do not change the receiver of future sales. Grant conditions and release of held USDC remain management's responsibility.

`recover funding --token ADDRESS --amount-raw INTEGER` prepares a funding recovery to the manager; add `--governance` for a **ychad.eth** clawback to governance. Recover yvUSDC-1 shares directly to avoid relying on vault redemption. Recovering vault shares also cancels pending funding payments. Recovery is independent of the seller and the vault's withdrawal availability; it does not reverse payments already made. The governance address is pinned to its ENS resolution verified on November 10, 2026, not resolved dynamically by the contract.

Funding amounts use 6-decimal USDC; proceeds quotes, minimums, balances and allocations also use 6-decimal USDC. The quote command returns `usdc`, keeper output uses `min_usdc`, and reports use `forwarded_usdc` / `withdrawn_usdc`.

## Dashboard

The dashboard is one page, [`dashboard/template.html`](dashboard/template.html), with no build step or external scripts. It shows a report from `vyper-grant report`, which reads every value at the latest mined block and rechecks that block's hash before writing. The token icons are embedded in the page: USDC from the [CoW Protocol token list](https://github.com/cowprotocol/token-lists) and CRV from [Llama Airforce](https://github.com/Llama-Airforce/Llama-Airforce).

The page leads with three figures from [budget request #356](https://github.com/yearn/budget/issues/356): Yearn's cumulative USDC proceeds against the 100,000 USDC target, its funding payments against the 100,000 USDC spending cap, and efficiency: proceeds per USDC spent, so 1.10× is $1.10 earned for every $1 spent. A week's payment lands at its start and its proceeds at its end, so efficiency counts completed weeks only, both in the headline and in each row of Weeks; the open week reads as pending. The chart plots proceeds and payments over time. Proceeds are realized USDC from sales; the request credits Vyper funding by valuing attributable CRV, which this page does not compute.

History lists each claim's split and every funding event: rounds, payments, cancellations, recipient changes, withdrawals and recoveries, with the recipient where there is one. Weeks sets out, for each week, Yearn's and other voters' veCRV on the gauge, the funding payment drawn in it and the result; the weights are the proceeds escrow's own snapshots, and the row for the coming week shows the votes standing so far. The report keeps funding payments out of its weekly rows, so the page places each in the week its round scheduled it for, or the week it was drawn when no round is on record. Info lists both escrows with their addresses and holdings.

```sh
uv run python -m vyper_grant.server             # live page at http://127.0.0.1:8787
uv run vyper-grant --out dashboard/dist report  # static index.html plus report.json
uv run vyper-grant --out dashboard/demo report --input dashboard/demo/report.json
```

The local server listens on loopback only and rebuilds the report when the head changes, checked every minute. A page served by it, or one with no report embedded, loads `report.json` from beside itself and polls it every ten seconds while the tab is visible. The header shows the report's age and turns amber once the block is ten minutes old. Pages written by `report` embed their data and do not poll. The last command regenerates the demo.

Hosting follows the stYFI dashboard: static files on Vercel and a collector on Electro. Neither is set up by this repository.

- **Page:** [`vercel.json`](vercel.json) publishes `dashboard/`, serves the template at `/` and forwards `/report.json` to `https://api.wavey.info/api/vyper-grant/report`.
- **Collector:** [`deploy/systemd`](deploy/systemd) runs `vyper-grant report` every minute as `svc-vyper-grant` against Electro's loopback node. It replaces `/var/lib/vyper-grant/report.json` atomically with mode 0644; a failed run leaves the previous file in place. Each run rescans events from `deployment_block`, so runs lengthen as the chain grows.
- **Route:** [`deploy/caddy/vyper-grant.caddy`](deploy/caddy/vyper-grant.caddy), imported by the `api.wavey.info` site, serves only that file.

Code and venv live in `/opt/vyper-grant`, root-owned and readable by `svc-vyper-grant`; install the package into the venv without the `dev` extra. The unit reads [`config/mainnet.json`](config/mainnet.json).

To update, copy the changed files from `master` into `/opt/vyper-grant` (root-owned, group `svc-vyper-grant`, mode 644), then reinstall and run once:

```sh
sudo uv pip install --python /opt/vyper-grant/.venv/bin/python --reinstall-package vyper-grant /opt/vyper-grant
sudo systemctl start vyper-grant-report.service
```

```sh
sudo install -m 644 deploy/systemd/vyper-grant-report.service deploy/systemd/vyper-grant-report.timer /etc/systemd/system/
sudo install -m 644 deploy/caddy/vyper-grant.caddy /etc/caddy/vyper-grant.caddy
sudo systemctl daemon-reload
sudo systemctl start vyper-grant-report.service
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl enable --now vyper-grant-report.timer
sudo journalctl -u vyper-grant-report.service -n 30
```
