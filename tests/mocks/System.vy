# pragma version ==0.4.3
interface Token:
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable
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
receiver: public(address)
activeRound: public(uint256)
maxExclusions: public(uint256)
is_killed: public(bool)
fail_market: public(bool)
last_campaign: public(Campaign)
last_excluded: public(DynArray[address, 50])
token: address
@deploy
def __init__(token: address, receiver: address):
    self.token = token
    self.receiver = receiver
    self.activeRound = 135
    self.maxExclusions = 50
@external
def settings(round: uint256, limit: uint256, killed: bool, fail: bool):
    self.activeRound = round
    self.maxExclusions = limit
    self.is_killed = killed
    self.fail_market = fail
@external
@view
def whitelistedPlatforms(platform: address) -> bool:
    return True
@external
def depositIncentive(token: address, amount: uint256, round: uint256, gauge: address, maxPerVote: uint256, excluded: DynArray[address, 50]):
    assert not self.fail_market, "market failed"
    assert round == self.activeRound
    self.last_excluded = excluded
    assert extcall Token(token).transferFrom(msg.sender, self, amount)
@external
@payable
def createCampaign(params: Campaign, destinationChainId: uint256, additionalGasLimit: uint256, votemarket: address):
    assert not self.fail_market, "market failed"
    assert destinationChainId == 42161 and additionalGasLimit == 600000
    self.last_campaign = params
    assert extcall Token(params.rewardToken).transferFrom(msg.sender, self, params.totalRewardAmount)
@external
def withdrawUnprocessed(round: uint256, gauge: address, incentive: uint256):
    assert extcall Token(self.token).transfer(msg.sender, incentive)
