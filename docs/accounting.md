# Weekly accounting

A gauge week starts at Thursday 00:00 UTC (`floor(timestamp / 604800) * 604800`). `B(w)` is its first finalized canonical Ethereum block. Each report stores that block and hash, the oracle round, update time, and raw answer. Require an answer above zero, an update timestamp no later than the block, and age at most 36 hours. The 24-hour feed heartbeat has some operational headroom. A bad oracle answer does not select a later favorable price: the row remains unresolved and planning holds.

Let `A(b)` be cumulative CRV accrued to the Vyper receiver from the gauge at block b. It is the Curve minter's cumulative minted value plus the gauge's simulated `claimable_tokens_write(receiver)` at b. Weekly gross emissions are `E(w) = A(B(w+1)) - A(B(w))`. As an independent check, `E(w)` must match `inflation_rate * gauge_relative_weight(w) * 604800 / 1e18` (capped by remaining `max_emissions`) within the emissions of the boundary blocks' few seconds of drift; the check is skipped in a week with an annual rate cut or a kill. Gauge cap and kill behavior are naturally reflected in this actual accrual. Snapshot and weight reads run in one transient Multicall3 call, since a checkpoint can fill controller data which is otherwise absent in storage.

Let `T(w)` be `GaugeController.points_weight(gauge, week).bias` after that transient checkpoint. This is the common unit for attribution. For a direct voter whose vote was effective at the start, its weight is `slope * max(lock_end - week, 0)`. Read `vote_user_slopes` from the block **before** B(w): a vote transaction in B(w) schedules next week's weight and can overwrite the old per-user record. New votes do not change this week's scheduled gauge bias.

For each week with a delivered Yearn VoteMarket campaign, `V(w)` is total gauge bias minus the unique excluded direct voters' weights. If several Yearn campaigns overlap, take the union of eligible voters by intersecting their exclusion sets. Convex's voter proxy must be excluded whenever Votium covers the same week; the planner requires it in every VoteMarket allocation. With no active VoteMarket campaign, V is zero. A completed week receives direct credit only if VoteMarket's oracle can settle a positive reward per vote. If voters have not yet called updateEpoch, a transient Multicall3 simulation updates each preceding campaign period and reads the result without a transaction. Missing oracle proofs leave the row pending. Its oracle total must match Curve's weekly gauge bias.

For a Yearn-funded Votium round, `C(w)` is Convex's actual effective Curve gauge vote. Its eligible fraction is `(all vlCVX votes for this gauge - excluded vlCVX votes) / all vlCVX votes for this gauge`. Finalized onchain Convex proposal totals corroborate Votium's submitted totals after accounting for their units. Votium supplies `excludedVotesReceived`; an independent check against directly visible Convex `getVote` data catches under-reported direct exclusions. Delegated votes use Votium's finalized excluded totals, the same authority used to distribute its incentives. The configurable `convex_voting_history` list supports an explicitly reviewed platform migration; one proposal must match the funded vote window uniquely.

A Votium round r opens at `(r + 1348) * 1209600`; the Curve emission weeks it votes for begin one week later and last two weeks. A VoteMarket campaign uses its actual Arbitrum start and end timestamps. Coverage is based on those actual periods, never a presumed Ethereum submission date. Votium incentives which receive no eligible votes can recycle; those child incentives are followed and the zero-vote parent buys no credit.

The exact rational credit fraction is:

`share(w) = (V(w) + C(w) * eligible_vlCVX / total_vlCVX) / T(w)`

With no Votium coverage its C component is zero. If T is zero, share is zero. Negative weights, excluded totals exceeding totals, and eligible weight exceeding T are errors. Do not silently clamp overlapping accounting. Apply the exact rational share before final integer rounding.

`credited_CRV(w) = E(w) * share(w)`

`received_gross_CRV(w) = max(0, min(A(B(w+1)), current_minter_minted) - A(B(w)))`

The second expression assigns cumulative minting in accrual order. It excludes old pre-program accrual even when minted during the program. Received credit is that value times share. With CRV in wei and a Chainlink price with eight decimals, received value in USDC base units is:

`floor(received_gross_CRV * share * price_answer / 10**20)`

An example: 10,000 CRV accrued; T=1,000; V=500; C=400; excluded vlCVX share=25%. Eligible gauge weight is 800; Yearn credit is 8,000 CRV. At $0.25/CRV that is $2,000. A 50% mint of this week's accrual yields $1,000 received value and $1,000 still accrued.

The convention credits every eligible non-pro-bono vote, including voters offered incentives by other sponsors. Technical VoteMarket exclusions prevent double payment through Convex without classifying all Convex votes as voluntary. No credit is assigned to a week without Yearn campaign coverage.

During an unfinished week, pending upper value includes actual gross accrual plus the current inflation rate times the fixed weekly relative weight for remaining seconds, capped by remaining gauge emissions. Annual inflation only reduces this rate. For unresolved attribution in a completed week, use gross emission value as the upper bound. If an opening price or accrual cannot be established, planning holds rather than inventing a bound. A confirmed target or fully allocated budget stops the program; confirmed plus pending upper value reaching target holds the next allocation pending settlement.

Campaign coverage in future, not-yet-started weeks is disclosed as prepaid commitments. The pending upper bound is for accrued or currently accruing emissions; future weekly prices and vote outcomes cannot be bounded economically without assuming the entire remaining gauge cap. Already prepaid campaigns can therefore overshoot the donation target. The contract's 100,000 USDC gross cap remains the independent loss bound.
