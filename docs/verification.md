# Verification

Verified November 11, 2026: **74 tests passed**, including an Ethereum fork at block **26,379,075**. No live transaction was sent. The `wavey3` deployment dry run also passed against Ethereum mainnet without unlocking the account.

```sh
uv sync --extra dev
uv run --no-sync vyper-grant --out build build
RUN_FORK_TESTS=1 uv run --no-sync pytest -q
uv run --no-sync python -m vyper_grant.deploy --account wavey3 --only proceeds \
  --config output/proceeds-v2.json --journal build/proceeds-v2.json
uv build
```

## Week boundaries

Both constructors take no arguments. Proceeds records its deployment week automatically and captures the current share if the controller's effective vote is still available. Tests cover deployment with zero Yearn votes, a subsequent vote that only applies next week, and a sale just before the boundary sending all output to Vyper. After the boundary, sales use the new week's share. There is no previous-week carryover rule or configured start/end date.

A sale quoted before a boundary can execute afterward and uses the new week's split. Contract and keeper integration tests carry an unchanged quote across a boundary where Yearn's share changes, and verify that the actual execution week determines allocation. The pull cap and absolute minimum remain unchanged, including when existing receiver CRV came from previous weeks. Tests explicitly distinguish that behavior from retaining the prior split. If a revote already overwrote the required voter history, checkpointing fails until management supplies the missing weight; snapshots cannot be overwritten afterward.

## Contracts and operations

The mainnet fork uses real USDC and yvUSDC-1, the real Yearn voter and GaugeController, the deployed gauge/minter, and TriCRV's CRV → crvUSD route. It exercises exact funding draws, vote changes, zero/nonzero allocation, capped pulls, boundary tails, forwarding and accumulation, and management/governance withdrawals. Existing multisig CRV is available to sell; capped pulls leave the remainder for later batches. Existing escrow balances stay outside the split. A separate anchored branch raises the gauge weight until the gauge reaches its emissions cap and verifies its final claim can still be sold. All impersonation and transactions remain inside the fork.

Contract tests cover exact vault withdrawals, share rounding, yield and top-ups, failure rollback, zero/partial/full Yearn shares, zero total gauge weight, public checkpointing while paused, bounded missing-weight recovery, existing token balances, unchanged absolute minimum output and the `max_crv` pull cap. Failed pulls, swaps, forwarding and returns roll back the claim and snapshot. With no existing CRV balance, missing mint permission leaves nothing to sell and also reverts. Tests drain capped leftovers after emissions stop, reject a zero cap, and verify that delayed claim growth cannot increase the amount pulled above the operator's cap. Cooldown tests cover the 600-second hard minimum, the exact eligibility boundary, shared timing across keepers and unchanged `last_sale` after failure.

Five-role tests exercise every privileged entry point as manager, governance, keeper, Vyper receiver and stranger. Manager and ychad.eth can withdraw any proceeds token to a chosen recipient while paused. Governance cannot change manager-only settings or sell unless separately authorized as a keeper. Public helpers cannot mint, transfer, change recipients or grant permissions.

Operations tests execute prepared Safe batches and keeper transactions against fixed-address dependency mocks. They cover independent funding/proceeds operation, archival vote recovery, current-week quoting, below-minimum closing sales, snapshots during cooldown, pending nonces and a lost response after a successful sale. Reports separate dated funding draws from weekly proceeds and distinguish accumulated, forwarded and withdrawn crvUSD.

## Deployment and verification

The deployment script uses the encrypted Brownie keystore through `eth_account`, prompts locally for its password and signs only the two no-argument mainnet deployments. Deployment itself grants no keeper permission. Tests use a generated test keystore and local chain; they cover a read-only dry run, both signed deployments, proceeds-only replacement while preserving funding and the original report block, saved addresses, verification-only retries and resuming an interrupted broadcast without duplicate deployment.

Before signing, the script compiles the exact Vyper standard JSON intended for Etherscan and compares its ABI, creation bytecode and runtime with the committed artifacts. The [Etherscan Vyper API](https://docs.etherscan.io/api-reference/endpoint/verifyvyper) request uses `vyper-json`, compiler `vyper:0.4.3` and empty constructor arguments. Mocked explorer tests cover indexing delays, queued verification, success, already-verified responses and rejection. Live source verification awaits deployment; the tests do not claim an Etherscan verification has already occurred.

A journal records each signed transaction hash before broadcasting. Resuming uses that hash; an unresolved transaction is not automatically replaced or redeployed. Source/build changes and conflicting configured deployments are rejected. Missing `ETHERSCAN_KEY` stops the broadcast path before unlocking the account.

Both contracts compile with Vyper 0.4.3. FundingEscrow is **57 lines**, with a **1,678-byte** runtime; its only mutable state is the recipient. ProceedsEscrow is **187 lines**, with a **6,039-byte** runtime; ordinary sales write only `last_sale` in escrow storage after a week's share is captured. Python compilation, shell syntax, whitespace checks and wheel/sdist builds pass. The contracts remain independent, and neither enforces grant milestones.

These checks validate execution-week settlement under the operating agreement, not precise earning-week attribution. Sell as close to the boundary as practical, respecting cooldown. Independent claimers must stop, and substantial missed claims require reconciliation. The earlier four-argument proceeds deployment remains unchanged; this two-argument API requires a new proceeds deployment and new approvals. Funding can be reused.
