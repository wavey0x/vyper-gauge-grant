# Vyper grant funding

Yearn's 100,000 USDC escrow for the renewed Vyper fundraising gauge. The management Safe allocates to Votium and VoteMarket once per Votium round; the treasury (`ychad.eth`) can recall unallocated USDC. No live deployment or keys are in this repository.

## Contract

`contracts/VyperFundingEscrow.vy`

- Every destination is immutable: USDC, gauge, Votium, the VoteMarket remote and platform, and the campaign manager.
- `allocate(votium, vote_market, max_reward_per_vote, votium_exclusions, vote_market_exclusions)`: management only, at most one per Votium round, at most 25,000 USDC per round and 100,000 USDC in total. Exclusion lists must be sorted and unique (at most 50). VoteMarket caps reward per vote and the bridge fee at 0.1 ETH.
- Votium and the remote get max USDC approval at deployment. Each allocation checks that the escrow balance fell by exactly the allocated amount.
- `withdrawUnprocessedVotium`: Votium refunds return to the escrow.
- `recallUSDC` / `recallETH`: treasury only.
- No upgrades, pause or arbitrary calls.

VoteMarket v2's Curve market exists only on Arbitrum. Allocations are sent on Ethereum, and the remote bridges each campaign over LaPoste to Arbitrum (`42161`). Voters claim there, and unused rewards are refunded there to the campaign manager as LaPoste-wrapped USDC.

## Setup

```sh
uv sync --extra dev
uv run vyper-grant build
uv run pytest
```

Fork tests use `ETHEREUM_RPC_URL` and `ARBITRUM_RPC_URL`, pinned to Ethereum block 26,336,208 and Arbitrum block 520,958,459:

```sh
anvil --fork-url "$ETHEREUM_RPC_URL" --fork-block-number 26336208 --port 8547 &
anvil --fork-url "$ARBITRUM_RPC_URL" --fork-block-number 520958459 --chain-id 42161 --port 8548 &
RUN_FORK_TESTS=1 OPS_FORK_RPC=http://127.0.0.1:8547 OPS_ARB_FORK_RPC=http://127.0.0.1:8548 uv run pytest
```

ABIs are vendored from verified deployments, and their provenance is in `abi/sources.json`.

## Operations

Copy `config/example.json` to `config/live.json` (ignored by git) and set `management` and `campaign_manager` (which defaults to `management`). `max_reward_per_vote` is the hard ceiling in USDC base units per full veCRV vote.

| Command | Output |
| --- | --- |
| `doctor` | Configuration blockers and the live Votium exclusion limit |
| `deploy --deployer <eoa>` | Unsigned deployment transaction |
| `plan-round --votium-usdc N --votemarket-usdc N --max-reward-per-vote N` | Reconciles, validates, quotes the bridge fee (+20%), simulates, then writes a Safe batch |
| `campaign update\|close --id N` | Arbitrum Safe batch to settle periods or recover unused rewards |
| `reconcile` | `output/report.json` rebuilt from finalized chain data |
| `report` | Static dashboard in `dashboard/dist` |

After deploying, set `escrow`, `deployment_block` and `arb_scan_from_block` in the config.

Exclusions live in the config: Votium lists pro bono vlCVX voters, and VoteMarket lists pro bono veCRV voters. VoteMarket must always include `addresses.convex_voter`. Votium's `maxExclusions()` is currently zero, so a nonempty Votium list needs its owner to raise the limit.

`ops/hourly.sh` runs `reconcile` and `report`. `plan-round` refuses to plan when reconciliation holds, meaning one of the following:

- A VoteMarket delivery is missing.
- Weekly evidence is missing.
- Confirmed value plus the pending upper bound could reach the target.

Any broken invariant fails reconciliation outright.

## Dashboard

`dashboard/template.html` is a static page with the report embedded, and it has no backend. To regenerate the illustrative demo:

```sh
uv run vyper-grant report --input dashboard/demo/report.json --out dashboard/demo
```

## Accounting

See [docs/accounting.md](docs/accounting.md). Every vote that is eligible for a Yearn-funded campaign after exclusions counts toward the target. Convex votes are converted into Curve gauge-weight units. Value counts only after Curve mints the CRV to Vyper's receiver.

## Sources

- [Vyper gauge proposal](https://gov.curve.finance/t/proposal-to-renew-vypers-vefunder-gauge/11160)
- [Curve gauges](https://docs.curve.finance/developer/gauges/overview/)
- [Chainlink historical prices](https://docs.chain.link/data-feeds/price-feeds/historical-data)
- [Votium](https://etherscan.io/address/0x63942E31E98f1833A234077f47880A66136a2D1e#code)
- [VoteMarket v2](https://github.com/stake-dao/votemarket-v2/blob/main/packages/votemarket/src/Votemarket.sol)
- [Convex voting](https://github.com/convex-eth/voting)
