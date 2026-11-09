# pragma version ==0.4.3
interface Minter:
    def minted(account: address, gauge: address) -> uint256: view

MINTER: constant(address) = 0xd061D61a4d941c39E5453435B6345Dc261C2fcE0

struct Point:
    bias: uint256
    slope: uint256
struct Vote:
    slope: uint256
    power: uint256
    end: uint256

receiver: public(address)
points_weight: public(HashMap[address, HashMap[uint256, Point]])
gauge_relative_weight: public(HashMap[address, HashMap[uint256, uint256]])
vote_user_slopes: public(HashMap[address, HashMap[address, Vote]])
last_user_vote: public(HashMap[address, HashMap[address, uint256]])
integrate_fraction: public(HashMap[address, uint256])
max_emissions: public(uint256)
inflation_rate: public(uint256)
future_epoch_time: public(uint256)
killed: bool
last_checkpoint: public(uint256)

@deploy
def __init__(receiver: address):
    self.receiver = receiver
    self.max_emissions = 2_000_000 * 10**18
    self.inflation_rate = 10**18 + 111
    self.future_epoch_time = block.timestamp + 365 * 86400
    self.last_checkpoint = block.timestamp

@external
def set_weight(week: uint256, bias: uint256, relative: uint256):
    self.points_weight[self][week] = Point(bias=bias, slope=0)
    self.gauge_relative_weight[self][week] = relative

@external
def set_vote(voter: address, slope: uint256, end: uint256, voted: uint256):
    self.vote_user_slopes[voter][self] = Vote(slope=slope, power=10000, end=end)
    self.last_user_vote[voter][self] = voted

@external
def set_params(cap: uint256, rate: uint256, epoch: uint256, killed: bool):
    self.max_emissions = cap
    self.inflation_rate = rate
    self.future_epoch_time = epoch
    self.killed = killed

@external
def checkpoint_gauge(gauge: address):
    pass

@internal
def _checkpoint():
    if self.integrate_fraction[self.receiver] == self.max_emissions:
        return
    start: uint256 = self.last_checkpoint
    rate: uint256 = self.inflation_rate
    epoch: uint256 = self.future_epoch_time
    for _: uint256 in range(68):
        if start == block.timestamp:
            break
        stop: uint256 = min(block.timestamp, (start // 604800 + 1) * 604800)
        weight: uint256 = self.gauge_relative_weight[self][start // 604800 * 604800]
        amount: uint256 = 0
        if start <= epoch and epoch < stop:
            amount = rate * weight * (epoch - start) // 10**18
            rate = rate * 10**18 // 1189207115002721024
            amount += rate * weight * (stop - epoch) // 10**18
            epoch += 365 * 86400
            self.inflation_rate = rate
            self.future_epoch_time = epoch
        else:
            amount = rate * weight * (stop - start) // 10**18
        if self.killed:
            amount = 0
        self.integrate_fraction[self.receiver] = min(self.max_emissions, self.integrate_fraction[self.receiver] + amount)
        if self.integrate_fraction[self.receiver] == self.max_emissions:
            break
        start = stop
    self.last_checkpoint = block.timestamp

@external
def user_checkpoint(account: address) -> bool:
    self._checkpoint()
    return True

@external
@view
def is_killed() -> bool:
    return self.killed or self.integrate_fraction[self.receiver] == self.max_emissions

@external
def claimable_tokens_write(account: address) -> uint256:
    self._checkpoint()
    return self.integrate_fraction[account] - staticcall Minter(MINTER).minted(account, self)
