# pragma version ==0.4.3
# pragma optimize codesize
"""Yearn USDC escrow. Management chooses amounts and exclusions, never recipients."""

interface Token:
    def balanceOf(account: address) -> uint256: view
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def transfer(receiver: address, amount: uint256) -> bool: nonpayable
interface Gauge:
    def receiver() -> address: view
    def is_killed() -> bool: view
interface Votium:
    def activeRound() -> uint256: view
    def maxExclusions() -> uint256: view
    def depositIncentive(token: address, amount: uint256, round: uint256, gauge: address, maxPerVote: uint256, excluded: DynArray[address, 50]): nonpayable
    def withdrawUnprocessed(round: uint256, gauge: address, incentive: uint256): nonpayable

struct Campaign:
    chainId: uint256
    gauge: address
    manager: address
    rewardToken: address
    numberOfPeriods: uint8
    maxRewardPerVote: uint256
    totalRewardAmount: uint256
    addresses: DynArray[address, 50]
    hook: address
    isWhitelist: bool
interface Remote:
    def whitelistedPlatforms(platform: address) -> bool: view
    def createCampaign(params: Campaign, destinationChainId: uint256, additionalGasLimit: uint256, votemarket: address): payable

event Allocated:
    round: indexed(uint256)
    votium_amount: uint256
    vote_market_amount: uint256
    max_reward_per_vote: uint256
    votium_exclusions: DynArray[address, 50]
    vote_market_exclusions: DynArray[address, 50]
event Recalled:
    amount: uint256

BUDGET: public(constant(uint256)) = 100_000 * 10**6
ROUND_CAP: public(constant(uint256)) = 25_000 * 10**6
MAX_BRIDGE_FEE: public(constant(uint256)) = 10**17
# VoteMarket v2 campaigns live on Arbitrum; the Ethereum remote bridges them there.
VOTE_MARKET_CHAIN: constant(uint256) = 42161
VOTE_MARKET_GAS: constant(uint256) = 600_000
TOKEN: public(immutable(address))
TREASURY: public(immutable(address))
MANAGEMENT: public(immutable(address))
GAUGE: public(immutable(address))
RECEIVER: public(immutable(address))
VOTIUM: public(immutable(address))
REMOTE: public(immutable(address))
VOTE_MARKET: public(immutable(address))
CAMPAIGN_MANAGER: public(immutable(address))
MAX_REWARD_PER_VOTE: public(immutable(uint256))

allocated: public(uint256)
used_rounds: public(HashMap[uint256, bool])

@deploy
def __init__(token: address, treasury: address, management: address, gauge: address, votium: address, remote: address, vote_market: address, campaign_manager: address, max_reward_per_vote: uint256):
    assert treasury != empty(address) and management != empty(address) and treasury != management, "roles"
    assert campaign_manager != empty(address) and max_reward_per_vote > 0, "campaign"
    assert staticcall Remote(remote).whitelistedPlatforms(vote_market), "platform"
    TOKEN = token
    TREASURY = treasury
    MANAGEMENT = management
    GAUGE = gauge
    RECEIVER = staticcall Gauge(gauge).receiver()
    assert RECEIVER != empty(address), "receiver"
    VOTIUM = votium
    REMOTE = remote
    VOTE_MARKET = vote_market
    CAMPAIGN_MANAGER = campaign_manager
    MAX_REWARD_PER_VOTE = max_reward_per_vote
    assert extcall Token(token).approve(votium, max_value(uint256))
    assert extcall Token(token).approve(remote, max_value(uint256))

@internal
@pure
def _check_list(accounts: DynArray[address, 50]):
    previous: uint256 = 0
    for account: address in accounts:
        assert convert(account, uint256) > previous, "sorted unique nonzero"
        previous = convert(account, uint256)

@external
@payable
@nonreentrant
def allocate(votium_amount: uint256, vote_market_amount: uint256, max_reward_per_vote: uint256, votium_exclusions: DynArray[address, 50], vote_market_exclusions: DynArray[address, 50]):
    assert msg.sender == MANAGEMENT, "management"
    round: uint256 = staticcall Votium(VOTIUM).activeRound()
    assert not self.used_rounds[round], "round used"
    total: uint256 = votium_amount + vote_market_amount
    assert total > 0 and total <= ROUND_CAP and self.allocated + total <= BUDGET, "budget"
    assert not staticcall Gauge(GAUGE).is_killed(), "gauge killed"
    assert staticcall Gauge(GAUGE).receiver() == RECEIVER, "receiver changed"
    self._check_list(votium_exclusions)
    self._check_list(vote_market_exclusions)
    if vote_market_amount > 0:
        assert max_reward_per_vote > 0 and max_reward_per_vote <= MAX_REWARD_PER_VOTE, "vote cap"
        assert msg.value > 0 and msg.value <= MAX_BRIDGE_FEE, "bridge fee"
    else:
        assert msg.value == 0 and max_reward_per_vote == 0 and len(vote_market_exclusions) == 0, "unused vm"
    self.used_rounds[round] = True
    self.allocated += total
    balance: uint256 = staticcall Token(TOKEN).balanceOf(self)
    if votium_amount > 0:
        assert len(votium_exclusions) <= staticcall Votium(VOTIUM).maxExclusions(), "exclusions disabled"
        extcall Votium(VOTIUM).depositIncentive(TOKEN, votium_amount, round, GAUGE, 0, votium_exclusions)
    if vote_market_amount > 0:
        params: Campaign = Campaign(chainId=1, gauge=GAUGE, manager=CAMPAIGN_MANAGER, rewardToken=TOKEN, numberOfPeriods=2, maxRewardPerVote=max_reward_per_vote, totalRewardAmount=vote_market_amount, addresses=vote_market_exclusions, hook=empty(address), isWhitelist=False)
        extcall Remote(REMOTE).createCampaign(params, VOTE_MARKET_CHAIN, VOTE_MARKET_GAS, VOTE_MARKET, value=msg.value)
    assert staticcall Token(TOKEN).balanceOf(self) + total == balance, "token delta"
    log Allocated(round=round, votium_amount=votium_amount, vote_market_amount=vote_market_amount, max_reward_per_vote=max_reward_per_vote, votium_exclusions=votium_exclusions, vote_market_exclusions=vote_market_exclusions)

@external
@nonreentrant
def withdrawUnprocessedVotium(round: uint256, incentive: uint256):
    assert msg.sender == MANAGEMENT, "management"
    extcall Votium(VOTIUM).withdrawUnprocessed(round, GAUGE, incentive)

@external
@nonreentrant
def recallUSDC(amount: uint256):
    assert msg.sender == TREASURY, "treasury"
    assert extcall Token(TOKEN).transfer(TREASURY, amount)
    log Recalled(amount=amount)

@external
@nonreentrant
def recallETH():
    assert msg.sender == TREASURY, "treasury"
    raw_call(TREASURY, b"", value=self.balance)

@external
@payable
def __default__():
    pass
