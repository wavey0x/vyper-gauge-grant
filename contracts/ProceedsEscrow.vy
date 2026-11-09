# pragma version ==0.4.3
# pragma optimize codesize
"""Sell funded-week gauge CRV. Keep Yearn's crvUSD; return Vyper's share atomically."""

interface Token:
    def balanceOf(account: address) -> uint256: view
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable

interface Funding:
    def MANAGER() -> address: view
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
    def allowed_to_mint_for(delegate: address, account: address) -> bool: view
    def mint_for(gauge: address, account: address): nonpayable

interface Pool:
    def coins(index: uint256) -> address: view
    def get_dy(i: uint256, j: uint256, amount: uint256) -> uint256: view
    def exchange(i: uint256, j: uint256, amount: uint256, min_out: uint256) -> uint256: nonpayable
    def price_oracle(index: uint256) -> uint256: view

struct Week:
    ready: bool
    yearn: uint256
    total: uint256
    earned: uint256
    sold: uint256
    output: uint256
    retained: uint256

event Activated:
    timestamp: uint256
    accrued: uint256
event Snapshot:
    week: indexed(uint256)
    slope: uint256
    end: uint256
    last_vote: uint256
    yearn: uint256
    total: uint256
    evidence: bytes32
event Accrued:
    week: indexed(uint256)
    crv: uint256
event Checkpoint:
    timestamp: uint256
    accrued: uint256
    excluded: uint256
event Sold:
    week: indexed(uint256)
    crv: uint256
    crvusd: uint256
    yearn: uint256
    vyper: uint256
event KeeperSet:
    account: indexed(address)
    enabled: bool
event LimitsSet:
    max_trade: uint256
    cooldown: uint256
    deviation_bps: uint256
event Paused:
    paused: bool
event Recovered:
    token: indexed(address)
    amount: uint256

WEEK: constant(uint256) = 604800
YEAR: constant(uint256) = 365 * 86400
# Leave sub-nano-CRV remnants with Vyper so zero-output dust cannot block FIFO.
DUST: public(constant(uint256)) = 10**9
FUNDING: public(immutable(address))
MANAGER: public(immutable(address))
START: public(immutable(uint256))
END: public(immutable(uint256))
CRV: public(immutable(address))
CRVUSD: public(immutable(address))
GAUGE: public(immutable(address))
CONTROLLER: public(immutable(address))
MINTER: public(immutable(address))
VOTER: public(immutable(address))
RECEIVER: public(immutable(address))
POOL: public(immutable(address))

weeks: public(HashMap[uint256, Week])
keepers: public(HashMap[address, bool])
paused: public(bool)
cursor: public(uint256)
accrued: public(uint256)
cap: public(uint256)
rate: uint256
rate_epoch: uint256
sale_week: uint256
max_trade: public(uint256)
cooldown: public(uint256)
deviation_bps: public(uint256)
last_sale: public(uint256)
total_earned: public(uint256)
total_sold: public(uint256)
total_output: public(uint256)
total_retained: public(uint256)

@deploy
def __init__(funding: address, crv: address, crvusd: address, gauge: address, controller: address, minter: address, voter: address, pool: address):
    FUNDING = funding
    MANAGER = staticcall Funding(funding).MANAGER()
    START = staticcall Funding(funding).START()
    END = staticcall Funding(funding).END()
    assert block.timestamp < START, "start"
    CRV = crv
    CRVUSD = crvusd
    GAUGE = gauge
    CONTROLLER = controller
    MINTER = minter
    VOTER = voter
    RECEIVER = staticcall Gauge(gauge).receiver()
    POOL = pool
    assert RECEIVER != empty(address) and voter != empty(address), "address"
    assert staticcall Pool(pool).coins(2) == crv and staticcall Pool(pool).coins(0) == crvusd, "route"
    assert extcall Token(crv).approve(pool, max_value(uint256)), "approve"
    self.sale_week = START

@external
@nonreentrant
def activate():
    assert msg.sender == MANAGER, "manager"
    assert self.cursor == 0 and block.timestamp < START, "activation"
    assert staticcall Minter(MINTER).allowed_to_mint_for(self, RECEIVER), "mint permission"
    assert not staticcall Gauge(GAUGE).is_killed(), "gauge killed"
    assert extcall Gauge(GAUGE).user_checkpoint(RECEIVER)
    extcall Minter(MINTER).mint_for(GAUGE, RECEIVER)
    self.cursor = block.timestamp
    self.accrued = staticcall Gauge(GAUGE).integrate_fraction(RECEIVER)
    assert staticcall Minter(MINTER).minted(RECEIVER, GAUGE) == self.accrued, "mint permission"
    self.cap = staticcall Gauge(GAUGE).max_emissions()
    self.rate = staticcall Gauge(GAUGE).inflation_rate()
    self.rate_epoch = staticcall Gauge(GAUGE).future_epoch_time()
    assert self.rate_epoch >= block.timestamp, "rate epoch"
    log Activated(timestamp=block.timestamp, accrued=self.accrued)

@internal
def _snapshot(week: uint256, slope: uint256, end: uint256, last_vote: uint256, evidence: bytes32):
    assert week >= START and week < END and week % WEEK == 0 and week <= block.timestamp, "week"
    assert staticcall Funding(FUNDING).paid(week) > 0, "unfunded"
    assert not self.weeks[week].ready and last_vote < week, "vote history"
    total: uint256 = (staticcall Controller(CONTROLLER).points_weight(GAUGE, week))[0]
    yearn: uint256 = slope * (max(end, week) - week)
    assert yearn <= total, "weight"
    self.weeks[week].ready = True
    self.weeks[week].yearn = yearn
    self.weeks[week].total = total
    log Snapshot(week=week, slope=slope, end=end, last_vote=last_vote, yearn=yearn, total=total, evidence=evidence)

@internal
def _capture(week: uint256):
    if not self.weeks[week].ready:
        vote: (uint256, uint256, uint256) = staticcall Controller(CONTROLLER).vote_user_slopes(VOTER, GAUGE)
        self._snapshot(week, vote[0], vote[2], staticcall Controller(CONTROLLER).last_user_vote(VOTER, GAUGE), empty(bytes32))

@external
@nonreentrant
def checkpoint_vote(week: uint256):
    extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    self._capture(week)

@external
@nonreentrant
def repair_week(week: uint256, slope: uint256, end: uint256, last_vote: uint256, evidence: bytes32):
    assert msg.sender == MANAGER and evidence != empty(bytes32), "manager evidence"
    extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    self._snapshot(week, slope, end, last_vote, evidence)

@external
@nonreentrant
def checkpoint() -> uint256:
    """Book earnings independently of minting or selling. At most 16 weeks of catch-up."""
    start: uint256 = self.cursor
    assert start > 0 and not self.paused, "inactive"
    if start >= END or start == block.timestamp:
        return 0
    assert block.timestamp - start <= 16 * WEEK, "catch-up limit"
    assert not staticcall Gauge(GAUGE).is_killed(), "gauge killed"
    assert staticcall Gauge(GAUGE).max_emissions() == self.cap, "cap changed"
    if self.accrued == self.cap:
        # The capped gauge returns early without filling controller history.
        extcall Controller(CONTROLLER).checkpoint_gauge(GAUGE)
    assert extcall Gauge(GAUGE).user_checkpoint(RECEIVER)
    accrued: uint256 = staticcall Gauge(GAUGE).integrate_fraction(RECEIVER)
    delta: uint256 = accrued - self.accrued
    remaining: uint256 = self.cap - self.accrued
    upper: uint256 = 0
    lower: uint256 = 0
    booked: uint256 = 0
    rate: uint256 = self.rate
    epoch: uint256 = self.rate_epoch
    one_week: bool = start // WEEK == (block.timestamp - 1) // WEEK
    # 16 weekly boundaries, at most one annual cut, and the final segment.
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
            eligible = 0
            if stop == block.timestamp:
                eligible = delta
        if week >= START and week < END and staticcall Funding(FUNDING).paid(week) > 0:
            self._capture(week)
            assert eligible == 0 or self.weeks[week].total > 0, "zero weight"
            self.weeks[week].earned += eligible
            booked += eligible
            if eligible > 0:
                log Accrued(week=week, crv=eligible)
        upper += u
        lower += l
        start = stop
    assert start == block.timestamp, "catch-up limit"
    assert min(remaining, lower) <= delta and delta <= min(remaining, upper), "accrual mismatch"
    if accrued < self.cap:
        assert staticcall Gauge(GAUGE).inflation_rate() == rate, "rate changed"
        assert staticcall Gauge(GAUGE).future_epoch_time() == epoch, "epoch changed"
    self.cursor = start
    self.accrued = accrued
    self.rate = rate
    self.rate_epoch = epoch
    self.total_earned += booked
    log Checkpoint(timestamp=start, accrued=accrued, excluded=delta - booked)
    return booked

@internal
@view
def _next() -> (uint256, uint256):
    week: uint256 = self.sale_week
    for _: uint256 in range(52):
        if week >= END or week > block.timestamp:
            break
        amount: uint256 = self.weeks[week].earned - self.weeks[week].sold
        if amount > DUST:
            return week, min(amount, self.max_trade)
        week += WEEK
    return 0, 0

@external
@view
def quote(amount: uint256) -> uint256:
    if amount == 0:
        return 0
    return staticcall Pool(POOL).get_dy(2, 0, amount)

@external
@view
def next_sale() -> (uint256, uint256):
    return self._next()

@external
@nonreentrant
def sell(week: uint256, amount: uint256, min_out: uint256, deadline: uint256):
    assert self.keepers[msg.sender] and not self.paused, "keeper or paused"
    assert block.timestamp <= deadline and block.timestamp >= self.last_sale + self.cooldown, "timing"
    next_: (uint256, uint256) = self._next()
    assert week == next_[0] and amount > 0 and amount <= next_[1], "sale amount"
    oracle: uint256 = staticcall Pool(POOL).price_oracle(1)
    assert oracle > 0 and min_out > 0, "price"
    assert min_out >= amount * oracle // 10**18 * (10000 - self.deviation_bps) // 10000, "price guard"
    self.sale_week = week
    self.last_sale = block.timestamp
    self.weeks[week].sold += amount
    self.total_sold += amount
    if staticcall Minter(MINTER).minted(RECEIVER, GAUGE) < self.accrued:
        extcall Minter(MINTER).mint_for(GAUGE, RECEIVER)
    assert staticcall Minter(MINTER).minted(RECEIVER, GAUGE) >= self.accrued, "mint permission"
    before_crv: uint256 = staticcall Token(CRV).balanceOf(self)
    before_out: uint256 = staticcall Token(CRVUSD).balanceOf(self)
    assert extcall Token(CRV).transferFrom(RECEIVER, self, amount), "pull"
    assert staticcall Token(CRV).balanceOf(self) == before_crv + amount, "input delta"
    extcall Pool(POOL).exchange(2, 0, amount, min_out)
    assert staticcall Token(CRV).balanceOf(self) == before_crv, "input spent"
    output: uint256 = staticcall Token(CRVUSD).balanceOf(self) - before_out
    assert output >= min_out, "output"
    self.weeks[week].output += output
    retained: uint256 = self.weeks[week].output * self.weeks[week].yearn // self.weeks[week].total
    yearn: uint256 = retained - self.weeks[week].retained
    self.weeks[week].retained = retained
    self.total_output += output
    self.total_retained += yearn
    assert extcall Token(CRVUSD).transfer(RECEIVER, output - yearn), "return"
    log Sold(week=week, crv=amount, crvusd=output, yearn=yearn, vyper=output - yearn)

@external
def set_keeper(account: address, enabled: bool):
    assert msg.sender == MANAGER and account != empty(address), "manager address"
    self.keepers[account] = enabled
    log KeeperSet(account=account, enabled=enabled)

@external
def set_limits(max_trade: uint256, cooldown: uint256, deviation_bps: uint256):
    assert msg.sender == MANAGER, "manager"
    assert max_trade > DUST and cooldown <= WEEK and deviation_bps <= 1000, "limits"
    self.max_trade = max_trade
    self.cooldown = cooldown
    self.deviation_bps = deviation_bps
    log LimitsSet(max_trade=max_trade, cooldown=cooldown, deviation_bps=deviation_bps)

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
