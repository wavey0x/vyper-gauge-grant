# pragma version ==0.4.3
# pragma optimize codesize
"""Sync funded-week CRV, sell it, and retain Yearn's share as crvUSD."""

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
    def gauge_relative_weight(gauge: address, week: uint256) -> uint256: view
    def vote_user_slopes(voter: address, gauge: address) -> (uint256, uint256, uint256): view
    def last_user_vote(voter: address, gauge: address) -> uint256: view

interface Gauge:
    def receiver() -> address: view
    def user_checkpoint(account: address) -> bool: nonpayable
    def integrate_fraction(account: address) -> uint256: view
    def max_emissions() -> uint256: view
    def inflation_rate() -> uint256: view
    def future_epoch_time() -> uint256: view
    def is_killed() -> bool: view

interface Minter:
    def minted(account: address, gauge: address) -> uint256: view
    def mint_for(gauge: address, account: address): nonpayable

interface Pool:
    def get_dy(i: uint256, j: uint256, amount: uint256) -> uint256: view
    def exchange(i: uint256, j: uint256, amount: uint256, min_out: uint256) -> uint256: nonpayable

struct Week:
    ready: bool
    yearn: uint256
    total: uint256
    remaining: uint256

event Snapshot:
    week: indexed(uint256)
    yearn: uint256
    total: uint256
    manual: bool
event Accrued:
    week: indexed(uint256)
    crv: uint256
event Synced:
    timestamp: uint256
    accrued: uint256
    excluded: uint256
event Sold:
    week: indexed(uint256)
    crv: uint256
    crvusd: uint256
    yearn: uint256
event KeeperSet:
    account: indexed(address)
    enabled: bool
event Paused:
    paused: bool
event Recovered:
    token: indexed(address)
    amount: uint256

WEEK: constant(uint256) = 604800
YEAR: constant(uint256) = 365 * 86400
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
CRV: public(constant(address)) = 0xD533a949740bb3306d119CC777fa900bA034cd52
CRVUSD: public(constant(address)) = 0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E
GAUGE: public(constant(address)) = 0x3b8EFAa0A1Fca3d31144002f6D9c7412B967E0b4
CONTROLLER: public(constant(address)) = 0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB
MINTER: public(constant(address)) = 0xd061D61a4d941c39E5453435B6345Dc261C2fcE0
VOTER: public(constant(address)) = 0xF147b8125d2ef93FB6965Db97D6746952a133934
POOL: public(constant(address)) = 0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14
FUNDING: public(immutable(address))
START: public(immutable(uint256))
END: public(immutable(uint256))
RECEIVER: public(immutable(address))
CAP: public(immutable(uint256))

weeks: public(HashMap[uint256, Week])
keepers: public(HashMap[address, bool])
paused: public(bool)
cursor: public(uint256)
accrued: public(uint256)
available: public(uint256)
sale_week: public(uint256)
rate: uint256
rate_epoch: uint256

@deploy
def __init__(funding: address):
    FUNDING = funding
    START = staticcall Funding(funding).START()
    END = staticcall Funding(funding).END()
    assert block.timestamp < START, "start"
    RECEIVER = staticcall Gauge(GAUGE).receiver()
    CAP = staticcall Gauge(GAUGE).max_emissions()
    assert RECEIVER != empty(address), "receiver"
    assert extcall Token(CRV).approve(POOL, max_value(uint256)), "approve"
    assert not staticcall Gauge(GAUGE).is_killed(), "gauge killed"
    assert extcall Gauge(GAUGE).user_checkpoint(RECEIVER)
    self.cursor = block.timestamp
    self.accrued = staticcall Gauge(GAUGE).integrate_fraction(RECEIVER)
    self.rate = staticcall Gauge(GAUGE).inflation_rate()
    self.rate_epoch = staticcall Gauge(GAUGE).future_epoch_time()
    assert self.rate_epoch >= block.timestamp, "rate epoch"
    self.sale_week = START

@internal
def _snapshot(week: uint256, yearn: uint256, manual: bool):
    assert week >= START and week < END and week % WEEK == 0 and week <= block.timestamp, "week"
    assert staticcall Funding(FUNDING).paid(week) > 0, "unfunded"
    assert not self.weeks[week].ready, "already captured"
    total: uint256 = (staticcall Controller(CONTROLLER).points_weight(GAUGE, week))[0]
    assert yearn <= total, "weight"
    self.weeks[week].ready = True
    self.weeks[week].yearn = yearn
    self.weeks[week].total = total
    log Snapshot(week=week, yearn=yearn, total=total, manual=manual)

@internal
def _capture(week: uint256):
    if not self.weeks[week].ready:
        assert staticcall Controller(CONTROLLER).last_user_vote(VOTER, GAUGE) < week, "missing vote history"
        vote: (uint256, uint256, uint256) = staticcall Controller(CONTROLLER).vote_user_slopes(VOTER, GAUGE)
        self._snapshot(week, vote[0] * (max(vote[2], week) - week), False)

@external
@nonreentrant
def set_missing_weight(week: uint256, yearn_weight: uint256):
    assert msg.sender == MANAGER, "manager"
    extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    self._snapshot(week, yearn_weight, True)

@internal
def _sync() -> uint256:
    start: uint256 = self.cursor
    if start >= END:
        return self.available
    assert block.timestamp - start <= 16 * WEEK, "catch-up limit"
    assert staticcall Gauge(GAUGE).max_emissions() == CAP, "cap changed"
    capped: bool = staticcall Gauge(GAUGE).integrate_fraction(RECEIVER) == CAP
    assert capped or not staticcall Gauge(GAUGE).is_killed(), "gauge killed"
    if capped:
        # A capped gauge returns early, so fill controller history ourselves.
        extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    assert extcall Gauge(GAUGE).user_checkpoint(RECEIVER)
    current: uint256 = block.timestamp // WEEK * WEEK
    if current >= START and current < END and staticcall Funding(FUNDING).paid(current) > 0:
        self._capture(current)
    if start == block.timestamp:
        return self.available
    accrued: uint256 = staticcall Gauge(GAUGE).integrate_fraction(RECEIVER)
    delta: uint256 = accrued - self.accrued
    remaining: uint256 = CAP - self.accrued
    upper: uint256 = 0
    lower: uint256 = 0
    booked: uint256 = 0
    rate: uint256 = self.rate
    epoch: uint256 = self.rate_epoch
    one_week: bool = start // WEEK == (block.timestamp - 1) // WEEK
    # At most 16 weekly boundaries, one annual cut, and the final segment.
    for _: uint256 in range(20):
        if start == block.timestamp:
            break
        if start == epoch:
            rate = rate * 10**18 // 1189207115002721024
            epoch += YEAR
        assert start < epoch, "rate history"
        week: uint256 = start // WEEK * WEEK
        stop: uint256 = min(block.timestamp, min(week + WEEK, epoch))
        k: uint256 = rate * staticcall Controller(CONTROLLER).gauge_relative_weight(GAUGE, week)
        u: uint256 = k * (stop - start) // 10**18
        l: uint256 = k // 10**18 * (stop - start)
        eligible: uint256 = min(l, remaining - min(remaining, upper))
        if one_week:
            eligible = delta if stop == block.timestamp else 0
        if eligible > 0 and week >= START and week < END and staticcall Funding(FUNDING).paid(week) > 0:
            self._capture(week)
            assert self.weeks[week].total > 0, "zero weight"
            self.weeks[week].remaining += eligible
            booked += eligible
            log Accrued(week=week, crv=eligible)
        upper += u
        lower += l
        start = stop
    assert start == block.timestamp, "catch-up limit"
    assert min(remaining, lower) <= delta and delta <= min(remaining, upper), "accrual mismatch"
    if accrued < CAP:
        assert staticcall Gauge(GAUGE).inflation_rate() == rate, "rate changed"
        assert staticcall Gauge(GAUGE).future_epoch_time() == epoch, "epoch changed"
    self.cursor = start
    if accrued != self.accrued:
        self.accrued = accrued
    if epoch != self.rate_epoch:
        self.rate = rate
        self.rate_epoch = epoch
    log Synced(timestamp=start, accrued=accrued, excluded=delta - booked)
    return self.available + booked

@external
@nonreentrant
def sync() -> uint256:
    available: uint256 = self._sync()
    if available != self.available:
        self.available = available
    return available

@external
@view
def quote(amount: uint256) -> uint256:
    return staticcall Pool(POOL).get_dy(2, 0, amount) if amount > 0 else 0

@external
@nonreentrant
def sell(amount: uint256, min_out: uint256):
    assert self.keepers[msg.sender] and not self.paused, "keeper or paused"
    available: uint256 = self._sync()
    assert amount > 0 and amount <= available, "sale amount"
    self.available = available - amount
    if staticcall Minter(MINTER).minted(RECEIVER, GAUGE) < self.accrued:
        extcall Minter(MINTER).mint_for(GAUGE, RECEIVER)
    assert staticcall Minter(MINTER).minted(RECEIVER, GAUGE) >= self.accrued, "mint permission"
    before: uint256 = staticcall Token(CRVUSD).balanceOf(self)
    assert extcall Token(CRV).transferFrom(RECEIVER, self, amount), "pull"
    extcall Pool(POOL).exchange(2, 0, amount, min_out)
    output: uint256 = staticcall Token(CRVUSD).balanceOf(self) - before
    week: uint256 = self.sale_week
    left: uint256 = amount
    allocated: uint256 = 0
    retained: uint256 = 0
    for _: uint256 in range(52):
        part: uint256 = min(left, self.weeks[week].remaining)
        if part > 0:
            self.weeks[week].remaining -= part
            left -= part
            proceeds: uint256 = output * part // amount if left > 0 else output - allocated
            yearn: uint256 = proceeds * self.weeks[week].yearn // self.weeks[week].total
            allocated += proceeds
            retained += yearn
            log Sold(week=week, crv=part, crvusd=proceeds, yearn=yearn)
        if left == 0:
            break
        week += WEEK
    assert left == 0, "inventory"
    if week != self.sale_week:
        self.sale_week = week
    assert extcall Token(CRVUSD).transfer(RECEIVER, output - retained), "return"

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
