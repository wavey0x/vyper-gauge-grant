# pragma version ==0.4.3
# pragma optimize codesize
"""@title Sell gauge rewards to USDC using the execution week's vote split."""

from ethereum.ercs import IERC20

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
    vyper_crv: uint256
event YearnReceiverSet:
    receiver: address
event KeeperSet:
    account: indexed(address)
    enabled: bool
event CooldownSet:
    seconds: uint256
event Paused:
    paused: bool
event Withdrawn:
    token: indexed(address)
    amount: uint256
    recipient: indexed(address)

WEEK: constant(uint256) = 604800
SCALE: constant(uint256) = 10**18
MIN_COOLDOWN: constant(uint256) = 600
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
GOVERNANCE: public(constant(address)) = 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52
CRV: public(constant(address)) = 0xD533a949740bb3306d119CC777fa900bA034cd52
CRVUSD: public(constant(address)) = 0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E
USDC: public(constant(address)) = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
GAUGE: public(constant(address)) = 0x3b8EFAa0A1Fca3d31144002f6D9c7412B967E0b4
CONTROLLER: public(constant(address)) = 0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB
MINTER: public(constant(address)) = 0xd061D61a4d941c39E5453435B6345Dc261C2fcE0
VOTER: public(constant(address)) = 0xF147b8125d2ef93FB6965Db97D6746952a133934
POOL: public(constant(address)) = 0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14
STABLE_POOL: public(constant(address)) = 0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E
DEPLOYED_WEEK: public(immutable(uint256))
RECEIVER: public(immutable(address))

weeks: public(HashMap[uint256, Week])
keepers: public(HashMap[address, bool])
paused: public(bool)
cooldown: public(uint256)
last_sale: public(uint256)
yearn_receiver: public(address)

@deploy
def __init__():
    """@notice Set up the fixed swap route and accumulate Yearn's USDC here."""
    DEPLOYED_WEEK = block.timestamp // WEEK * WEEK
    RECEIVER = staticcall Gauge(GAUGE).receiver()
    assert RECEIVER != empty(address), "receiver"
    self.yearn_receiver = self
    self.cooldown = MIN_COOLDOWN
    assert extcall IERC20(CRV).approve(POOL, max_value(uint256)), "approve"
    assert extcall IERC20(CRVUSD).approve(STABLE_POOL, max_value(uint256)), "approve"
    if staticcall Controller(CONTROLLER).last_user_vote(VOTER, GAUGE) < DEPLOYED_WEEK:
        self._checkpoint(DEPLOYED_WEEK)

@internal
def _snapshot(week: uint256, yearn: uint256, manual: bool):
    assert week >= DEPLOYED_WEEK and week % WEEK == 0 and week <= block.timestamp, "week"
    assert not self.weeks[week].ready, "already captured"
    total: uint256 = (staticcall Controller(CONTROLLER).points_weight(GAUGE, week))[0]
    assert yearn <= total, "weight"
    self.weeks[week] = Week(ready=True, share=yearn * SCALE // total if total > 0 else 0)
    log Snapshot(week=week, yearn=yearn, total=total, manual=manual)

@internal
def _checkpoint(week: uint256) -> uint256:
    if not self.weeks[week].ready:
        extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
        assert staticcall Controller(CONTROLLER).last_user_vote(VOTER, GAUGE) < week, "missing vote history"
        vote: (uint256, uint256, uint256) = staticcall Controller(CONTROLLER).vote_user_slopes(VOTER, GAUGE)
        self._snapshot(week, vote[0] * (max(vote[2], week) - week), False)
    return self.weeks[week].share

@external
@nonreentrant
def checkpoint() -> uint256:
    """
    @notice Freeze the current week's share without claiming or selling.
    @return Yearn's fraction of the gauge weight, scaled by 1e18.
    """
    return self._checkpoint(block.timestamp // WEEK * WEEK)

@external
@nonreentrant
def set_missing_weight(week: uint256, yearn_weight: uint256):
    """
    @notice Let the manager fill a missing weekly snapshot.
    @param week UTC week-start timestamp, a multiple of 604800.
    @param yearn_weight Yearn's effective gauge weight at that boundary.
    """
    assert msg.sender == MANAGER, "manager"
    extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    self._snapshot(week, yearn_weight, True)

@external
def claimable() -> uint256:
    """
    @notice Simulate with eth_call to include pending rewards without persisting the gauge checkpoint.
    @return Receiver CRV balance plus pending rewards, in 18-decimal CRV units.
    """
    return staticcall IERC20(CRV).balanceOf(RECEIVER) + extcall Gauge(GAUGE).claimable_tokens_write(RECEIVER)

@external
@view
def quote(amount: uint256) -> uint256:
    """
    @notice Quote the fixed CRV to crvUSD to USDC route.
    @param amount CRV to swap, in 18-decimal units.
    @return Expected USDC output, in 6-decimal units.
    """
    if amount == 0:
        return 0
    intermediate: uint256 = staticcall Pool(POOL).get_dy(2, 0, amount)
    return staticcall StablePool(STABLE_POOL).get_dy(1, 0, intermediate) if intermediate > 0 else 0

@external
@view
def canWork() -> bool:
    """@return Whether work is unpaused and the sale cooldown has elapsed."""
    return not self.paused and block.timestamp - self.last_sale >= self.cooldown

@external
@nonreentrant
def work(min_out: uint256, max_crv: uint256, sell_vyper: bool):
    """
    @notice Claim a capped batch and allocate it using the execution week's share.
    @param min_out Minimum final USDC output, in 6-decimal units; zero when no CRV is sold.
    @param max_crv Maximum CRV to pull from Vyper, in 18-decimal units.
    @param sell_vyper True sells Vyper's portion to USDC; False retains it here as CRV.
    """
    assert self.keepers[msg.sender] and not self.paused, "keeper or paused"
    assert block.timestamp - self.last_sale >= self.cooldown, "cooldown"
    week: uint256 = block.timestamp // WEEK * WEEK
    share: uint256 = self._checkpoint(week)
    extcall Minter(MINTER).mint_for(GAUGE, RECEIVER)
    amount: uint256 = min(staticcall IERC20(CRV).balanceOf(RECEIVER), max_crv)
    assert amount > 0, "nothing to sell"
    assert extcall IERC20(CRV).transferFrom(RECEIVER, self, amount), "pull"
    swap_amount: uint256 = amount if sell_vyper else amount * share // SCALE
    output: uint256 = 0
    if swap_amount > 0:
        intermediate: uint256 = extcall Pool(POOL).exchange(2, 0, swap_amount, 0)
        output = extcall StablePool(STABLE_POOL).exchange(1, 0, intermediate, min_out)
    else:
        assert min_out == 0, "min out"
    yearn: uint256 = output * share // SCALE if sell_vyper else output
    receiver: address = self.yearn_receiver
    if yearn > 0 and receiver != self:
        assert extcall IERC20(USDC).transfer(receiver, yearn), "forward"
    if output > yearn:
        assert extcall IERC20(USDC).transfer(RECEIVER, output - yearn), "return"
    self.last_sale = block.timestamp
    log Sold(week=week, crv=swap_amount, usdc=output, yearn=yearn, yearn_receiver=receiver, vyper_crv=amount - swap_amount)

@external
def set_yearn_receiver(receiver: address):
    """
    @notice Set the destination of future Yearn USDC proceeds.
    @param receiver Nonzero recipient; use this escrow to accumulate here.
    """
    assert msg.sender == MANAGER and receiver != empty(address), "manager address"
    self.yearn_receiver = receiver
    log YearnReceiverSet(receiver=receiver)

@external
def set_keeper(account: address, enabled: bool):
    """
    @notice Set an account's permission to call work.
    @param account Nonzero keeper address.
    @param enabled Whether the account may call work.
    """
    assert msg.sender == MANAGER and account != empty(address), "manager address"
    self.keepers[account] = enabled
    log KeeperSet(account=account, enabled=enabled)

@external
def set_cooldown(seconds: uint256):
    """@param seconds Minimum time between successful work calls; at least 600."""
    assert msg.sender == MANAGER, "manager"
    assert seconds >= MIN_COOLDOWN, "cooldown"
    self.cooldown = seconds
    log CooldownSet(seconds=seconds)

@external
def set_paused(paused: bool):
    """@param paused True stops work; checkpoint and withdrawals remain available."""
    assert msg.sender == MANAGER, "manager"
    self.paused = paused
    log Paused(paused=paused)

@external
@nonreentrant
def withdraw(token: address, amount: uint256, recipient: address):
    """
    @notice Withdraw as management, or as the Vyper multisig for CRV only.
    @param token ERC20 token to withdraw.
    @param amount Amount in the token's smallest units.
    @param recipient Nonzero destination other than this escrow.
    """
    if token == CRV:
        assert msg.sender == RECEIVER, "vyper"
    else:
        assert msg.sender == MANAGER or msg.sender == GOVERNANCE, "management"
    assert recipient != empty(address) and recipient != self, "recipient"
    assert extcall IERC20(token).transfer(recipient, amount, default_return_value=True), "transfer"
    log Withdrawn(token=token, amount=amount, recipient=recipient)
