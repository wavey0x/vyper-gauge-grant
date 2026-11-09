# Vyper funding

Two Vyper contracts on Ethereum. No vote marketplace, bridge, router or frontend build.

- **FundingEscrow** holds **yvUSDC-1** shares. The manager purchases a future emission week by withdrawing an exact USDC amount to a fixed OTC recipient. Each week can be paid once; lifetime spending cannot exceed **100,000 USDC**. Yield and recovered funds never reset that counter.
- **ProceedsEscrow** sells funded-week CRV through the fixed CRV/crvUSD pool. It keeps Yearn's share as **crvUSD** and returns the other share to Vyper in the same transaction. A weekly ledger preserves the earning week through delayed minting and sales.

Both contracts are managed by `0x4444AAAACDBa5580282365e25b16309Bd770ce4a`. Keepers can sell; only the manager changes permissions or limits and recovers tokens to that multisig. There is no arbitrary execution or upgrade mechanism.

The dashboard has two balances and one weekly settlement table. Open [the illustrative report](dashboard/demo/index.html), or see [the preview](dashboard/preview.png).

## Local checks

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
uv run --no-sync pytest
RUN_FORK_TESTS=1 uv run --no-sync pytest tests/test_fork.py
```

The fork test uses `ETHEREUM_RPC_URL` and never submits live transactions. See [verification](docs/verification.md) and [accounting](docs/accounting.md).

## Deploy and configure

Copy `config/example.json` to `config/live.json`. Set the agreed OTC **payee**, a future Thursday 00:00 UTC **start** timestamp, the programme duration (1–52 weeks), and a dedicated keeper address. The payee is deliberately unset: it must be the agreed yCRV OTC payment recipient, not Vyper or TheDAO by assumption. Production token, vault, gauge, pool, voter and manager addresses are fixed in the tooling.

```sh
uv run vyper-grant deploy funding --deployer YOUR_ADDRESS
# Submit the unsigned deployment through your signer; record funding and deployment_block.
uv run vyper-grant deploy proceeds --deployer YOUR_ADDRESS
# Record proceeds, then verify both deployments:
uv run vyper-grant doctor
uv run vyper-grant setup --allowance-crv AGREED_CRV_ALLOWANCE
```

Deploy commands only prepare unsigned transactions. Setup writes two Safe batches. Vyper executes its CRV allowance and conditional mint authorization first; the manager then sets sale limits, enables the keeper, and activates before the first earning week. Activation claims legacy emissions into Vyper's wallet and excludes them from the programme.

Fund the funding contract by transferring yvUSDC-1 shares. The vault is `0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204`; `previewWithdraw(100000000000)` gives the shares needed for approximately 100,000 USDC at the current price. Recheck at funding time. Raw USDC sent to the contract is recoverable, but is not automatically invested or spent. Vault losses or unavailable withdrawal liquidity stop purchases; withdrawals permit zero loss.

## Weekly purchases and voting

```sh
uv run vyper-grant purchase --week THURSDAY_TIMESTAMP --usdc 2000 --reference AGREEMENT_OR_RATE_REFERENCE
uv run vyper-grant checkpoint-vote --week CURRENT_WEEK_TIMESTAMP
```

These commands simulate and prepare Safe batches. The reference string and its hash are included in the purchase batch. A purchase authorizes attribution for that week, assuming **all** of the designated Yearn voter's gauge weight was bought. It does not cast a Curve vote or verify the commercial price. Coordinate payment with the existing yCRV voting workflow; the escrow manager is not the voter governance address. Before replacing an active week's vote, execute `checkpoint-vote` first or include that call before the vote in the voting Safe's atomic batch. The checkpoint itself is permissionless.

The manager decides when the grant's offchain conditions permit spending and when to stop purchases. The contract enforces the lifetime spending budget; it does not enforce grant milestones or treat crvUSD output as USDC expenditure.

## Keeper and reports

```sh
uv run vyper-grant keeper                 # simulate one action
uv run vyper-grant keeper --send          # uses KEEPER_PRIVATE_KEY
uv run vyper-grant reconcile
uv run vyper-grant --out dashboard/dist report
```

Schedule `ops/run.sh` every five minutes with `VYPER_GRANT_DIR` and `ETHEREUM_RPC_URL` set externally. Set `KEEPER_SEND=1` and `KEEPER_PRIVATE_KEY` only for the dedicated keeper. Nothing is scheduled by this repository. Each pass submits at most one transaction: checkpoint accounting, then sell on a subsequent pass. Already-booked CRV sells before new accounting. Small final-week remainders are settled even below the normal trade minimum.

Keep accounting current: each checkpoint accepts at most 16 weeks of elapsed time. A longer gap stops new accrual rather than guessing its allocation. Capture the weekly voter snapshot before any revote even when trading is paused.

The keeper uses a file lock and a signed-transaction journal under `output/keeper`. It persists before broadcasting, rebroadcasts the same transaction after an uncertain send, and waits for two confirmations before another nonce. Use one host and signer per keeper; an untracked pending nonce stops execution. It does not automatically increase fees or replace expired sales. Resolve a stuck transaction with the same nonce and reconcile its receipt before manually retiring its journal. Dashboard accounting uses finalized blocks, so it intentionally lags execution.

`quote`, `checkpoint`, `limits`, `set-keeper --account ADDRESS [--disable]`, `set-paused true|false`, and `recover funding|proceeds --token ADDRESS --amount-raw INTEGER` provide the remaining operations. Manager commands produce unsigned Safe batches. `repair-week --week TIMESTAMP` prepares archival evidence and a manager recovery batch for a missed vote snapshot; see the trust boundary in [accounting](docs/accounting.md).

Example limits are starting configuration, not calibrated production approval. Verify the OTC recipient, permissions, price tolerance and trade size, then perform a small sale before increasing limits. There is no live deployment recorded in this checkout.
