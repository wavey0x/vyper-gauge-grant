# Implemented refactor

The implementation follows the latest instruction: yvUSDC-1 programme funding and crvUSD proceeds, in two Vyper contracts.

- `FundingEscrow` holds vault shares and enforces an immutable 100,000 USDC lifetime payment budget. Each payment purchases one future emission week and goes to a fixed OTC recipient.
- `ProceedsEscrow` reads paid weeks from funding, captures weekly Curve voter bias, books eligible CRV, and sells through one fixed pool. Yearn's share stays as crvUSD; Vyper's share is returned atomically.
- The manager is the specified Yearn 0x444 multisig. Bots have sale permission only. Both contracts allow manager recovery to that multisig.
- Historical snapshots are immutable. Missing voter history requires explicit manager recovery supported by archival evidence. Keepers cannot assert ratios or adjust accrual.
- Accounting checkpoints are separate from sale execution. This makes the routine API `checkpoint`, `next_sale`, `quote`, `sell`, without duplicating a complex pending-accrual model in a view.
- Operations are Ethereum-only. Votium, VoteMarket campaigns, bridge fees, Convex attribution and oracle-valued donation credit are removed.
- The dashboard displays funding held, proceeds held, and weekly settlement. Reports use actual finalized events and exact token amounts.

The source of operational truth is now [README](../README.md), [accounting](accounting.md), and [verification](verification.md). The earlier proposal to keep the purchase budget entirely outside the contract is superseded by the dedicated funding escrow. Deployment and live spending remain separate from this local implementation.
