# pragma version ==0.4.3
# pragma optimize codesize
"""Sell each fresh CRV claim using its execution week's frozen Yearn share."""

interface Token:
    def balanceOf(account: address) -> uint256: view
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable

interface Funding:
    def START() -> uint256: view
    def END() -> uint256: view
    def paid(week: uint256) -> uint256: view

interface Controller:
    def checkpoint_gauge(gauge: address): nonpayable
    def points_weight(gauge: address, week: uint256) -> (uint256, uint256): view
    def vote_user_slopes(voter: address, gauge: address) -> (uint256, uint256, uint256): view
    def last_user_vote(voter: address, gauge: address) -> uint256: view

interface Gauge:
    def receiver() -> address: view
    def claimable_tokens_write(account: address) -> uint256: nonpayable

interface Minter:
    def mint_for(gauge: address, account: address): nonpayable

interface Pool:
    def get_dy(i: uint256, j: uint256, amount: uint256) -> uint256: view
    def exchange(i: uint256, j: uint256, amount: uint256, min_out: uint256) -> uint256: nonpayable

interface StablePool:
    def get_dy(i: int128, j: int128, amount: uint256) -> uint256: view
    def exchange(i: int128, j: int128, amount: uint256, min_out: uint256) -> uint256: nonpayable

struct Week:
    ready: bool
    share: uint256

event Snapshot:
    week: indexed(uint256)
    yearn: uint256
    total: uint256
    manual: bool
event Sold:
    week: indexed(uint256)
    crv: uint256
    usdc: uint256
    yearn: uint256
    yearn_receiver: address
event YearnReceiverSet:
    receiver: address
event KeeperSet:
    account: indexed(address)
    enabled: bool
event Paused:
    paused: bool
event Recovered:
    token: indexed(address)
    amount: uint256

WEEK: constant(uint256) = 604800
SCALE: constant(uint256) = 10**18
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
CRV: public(constant(address)) = 0xD533a949740bb3306d119CC777fa900bA034cd52
CRVUSD: public(constant(address)) = 0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E
USDC: public(constant(address)) = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
GAUGE: public(constant(address)) = 0x3b8EFAa0A1Fca3d31144002f6D9c7412B967E0b4
CONTROLLER: public(constant(address)) = 0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB
MINTER: public(constant(address)) = 0xd061D61a4d941c39E5453435B6345Dc261C2fcE0
VOTER: public(constant(address)) = 0xF147b8125d2ef93FB6965Db97D6746952a133934
POOL: public(constant(address)) = 0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14
STABLE_POOL: public(constant(address)) = 0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E
FUNDING: public(immutable(address))
START: public(immutable(uint256))
END: public(immutable(uint256))
RECEIVER: public(immutable(address))

weeks: public(HashMap[uint256, Week])
keepers: public(HashMap[address, bool])
paused: public(bool)
yearn_receiver: public(address)

@deploy
def __init__(funding: address):
    FUNDING = funding
    START = staticcall Funding(funding).START()
    END = staticcall Funding(funding).END()
    assert block.timestamp < START, "start"
    RECEIVER = staticcall Gauge(GAUGE).receiver()
    assert RECEIVER != empty(address), "receiver"
    self.yearn_receiver = self
    assert extcall Token(CRV).approve(POOL, max_value(uint256)), "approve"
    assert extcall Token(CRVUSD).approve(STABLE_POOL, max_value(uint256)), "approve"

@internal
def _snapshot(week: uint256, yearn: uint256, manual: bool):
    assert week >= START and week < END and week % WEEK == 0 and week <= block.timestamp, "week"
    assert staticcall Funding(FUNDING).paid(week) > 0, "unfunded"
    assert not self.weeks[week].ready, "already captured"
    total: uint256 = (staticcall Controller(CONTROLLER).points_weight(GAUGE, week))[0]
    assert yearn <= total, "weight"
    self.weeks[week] = Week(ready=True, share=yearn * SCALE // total if total > 0 else 0)
    log Snapshot(week=week, yearn=yearn, total=total, manual=manual)

@internal
def _checkpoint(week: uint256) -> uint256:
    if week < START or week >= END or staticcall Funding(FUNDING).paid(week) == 0:
        return 0
    if not self.weeks[week].ready:
        extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
        assert staticcall Controller(CONTROLLER).last_user_vote(VOTER, GAUGE) < week, "missing vote history"
        vote: (uint256, uint256, uint256) = staticcall Controller(CONTROLLER).vote_user_slopes(VOTER, GAUGE)
        self._snapshot(week, vote[0] * (max(vote[2], week) - week), False)
    return self.weeks[week].share

@external
@nonreentrant
def checkpoint() -> uint256:
    """Freeze the current week's share, even while sales are paused."""
    return self._checkpoint(block.timestamp // WEEK * WEEK)

@external
@nonreentrant
def set_missing_weight(week: uint256, yearn_weight: uint256):
    assert msg.sender == MANAGER, "manager"
    extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    self._snapshot(week, yearn_weight, True)

@external
def claimable() -> uint256:
    """Checkpoint the gauge and return pending CRV; use eth_call to simulate."""
    return extcall Gauge(GAUGE).claimable_tokens_write(RECEIVER)

@external
@view
def quote(amount: uint256) -> uint256:
    if amount == 0:
        return 0
    intermediate: uint256 = staticcall Pool(POOL).get_dy(2, 0, amount)
    return staticcall StablePool(STABLE_POOL).get_dy(1, 0, intermediate) if intermediate > 0 else 0

@external
@nonreentrant
def sell(min_out: uint256, expected_week: uint256, quoted_crv: uint256, max_crv: uint256):
    assert self.keepers[msg.sender] and not self.paused, "keeper or paused"
    assert quoted_crv > 0 and max_crv >= quoted_crv, "quote"
    week: uint256 = block.timestamp // WEEK * WEEK
    assert week == expected_week, "stale week"
    share: uint256 = self._checkpoint(week)
    before: uint256 = staticcall Token(CRV).balanceOf(RECEIVER)
    extcall Minter(MINTER).mint_for(GAUGE, RECEIVER)
    amount: uint256 = staticcall Token(CRV).balanceOf(RECEIVER) - before
    assert amount > 0, "nothing minted"
    assert amount <= max_crv, "claim too large"
    assert extcall Token(CRV).transferFrom(RECEIVER, self, amount), "pull"
    before = staticcall Token(USDC).balanceOf(self)
    intermediate: uint256 = extcall Pool(POOL).exchange(2, 0, amount, 0)
    extcall StablePool(STABLE_POOL).exchange(1, 0, intermediate, min_out * amount // quoted_crv)
    output: uint256 = staticcall Token(USDC).balanceOf(self) - before
    yearn: uint256 = output * share // SCALE
    receiver: address = self.yearn_receiver
    if yearn > 0 and receiver != self:
        assert extcall Token(USDC).transfer(receiver, yearn), "forward"
    if output > yearn:
        assert extcall Token(USDC).transfer(RECEIVER, output - yearn), "return"
    log Sold(week=week, crv=amount, usdc=output, yearn=yearn, yearn_receiver=receiver)

@external
def set_yearn_receiver(receiver: address):
    assert msg.sender == MANAGER and receiver != empty(address), "manager address"
    self.yearn_receiver = receiver
    log YearnReceiverSet(receiver=receiver)

@external
def set_keeper(account: address, enabled: bool):
    assert msg.sender == MANAGER and account != empty(address), "manager address"
    self.keepers[account] = enabled
    log KeeperSet(account=account, enabled=enabled)

@external
def set_paused(paused: bool):
    assert msg.sender == MANAGER, "manager"
    self.paused = paused
    log Paused(paused=paused)

@external
@nonreentrant
def recover(token: address, amount: uint256):
    assert msg.sender == MANAGER, "manager"
    assert extcall Token(token).transfer(MANAGER, amount, default_return_value=True), "transfer"
    log Recovered(token=token, amount=amount)
