# Verification

Verified on November 3, 2026 against local anvil forks of Ethereum block 26,336,208 and Arbitrum block 520,958,459. All 33 tests pass. No live transaction was sent.

Covered:

- **Roles**: management cannot withdraw escrow USDC, and only the treasury can recall.
- **Allocation gates**:
  - one allocation per round, the per-round and total caps, and no recycling of the budget
  - exclusion ordering and uniqueness, and Votium's exclusion limit
  - the gauge-kill check, the VoteMarket reward-per-vote and bridge-fee bounds
  - full rollback when a market call fails
- **Real market calls**: Votium deposits and VoteMarket remote submissions, separately and mixed, with an actual LaPoste/CCIP fee quote.
- **Ethereum to Arbitrum**:
  - LaPoste delivery to Arbitrum, authenticated by impersonating the adapter
  - campaign creation, then a management-Safe close batch
  - reconciliation of the verified refund, with correlation by LaPoste nonce and payload hash
- **VoteMarket settlement**: a read-only simulation of an unprocessed VoteMarket period.
- **Accounting**: exact rational credit, vlCVX conversion, overlap rejection, partial minting and rounding.
- **Report integrity**: rejection of a reorganized source block, and script-safe dashboard data embedding.

Boundaries:

- Fork delivery does not prove that the external CCIP relayer is live.
- Votium's finalized settlement is the authority for delegated excluded-vote totals. Directly visible exclusions and the aggregate Convex totals are cross-checked against it.
