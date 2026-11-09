# pragma version ==0.4.3
"""A fixed USDC budget held as yvUSDC-1. One exact payment per future week."""

interface Token:
    def balanceOf(account: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

interface Vault:
    def asset() -> address: view
    def convertToAssets(shares: uint256) -> uint256: view
    def withdraw(assets: uint256, receiver: address, owner: address, max_loss: uint256) -> uint256: nonpayable

event Purchased:
    week: indexed(uint256)
    usdc: uint256
    shares: uint256

event Recovered:
    token: indexed(address)
    amount: uint256

WEEK: constant(uint256) = 604800
BUDGET: public(constant(uint256)) = 100_000 * 10**6
VAULT: public(immutable(address))
USDC: public(immutable(address))
MANAGER: public(immutable(address))
PAYEE: public(immutable(address))
START: public(immutable(uint256))
END: public(immutable(uint256))
spent: public(uint256)
paid: public(HashMap[uint256, uint256])

@deploy
def __init__(vault: address, manager: address, payee: address, start: uint256, weeks: uint256):
    assert manager != empty(address) and payee != empty(address), "address"
    assert start > block.timestamp and start % WEEK == 0, "start"
    assert weeks > 0 and weeks <= 52, "duration"
    VAULT = vault
    USDC = staticcall Vault(vault).asset()
    MANAGER = manager
    PAYEE = payee
    START = start
    END = start + weeks * WEEK

@external
@view
def assets() -> uint256:
    return staticcall Vault(VAULT).convertToAssets(staticcall Token(VAULT).balanceOf(self))

@external
@view
def remaining() -> uint256:
    return BUDGET - self.spent

@external
@nonreentrant
def purchase(week: uint256, amount: uint256):
    assert msg.sender == MANAGER, "manager"
    assert week >= START and week < END and week % WEEK == 0, "week"
    assert week > block.timestamp and self.paid[week] == 0, "future unpaid week"
    assert amount > 0 and self.spent + amount <= BUDGET, "budget"
    self.paid[week] = amount
    self.spent += amount
    before: uint256 = staticcall Token(USDC).balanceOf(PAYEE)
    shares: uint256 = extcall Vault(VAULT).withdraw(amount, PAYEE, self, 0)
    assert staticcall Token(USDC).balanceOf(PAYEE) - before == amount, "payment"
    log Purchased(week=week, usdc=amount, shares=shares)

@external
@nonreentrant
def recover(token: address, amount: uint256):
    assert msg.sender == MANAGER, "manager"
    assert extcall Token(token).transfer(MANAGER, amount, default_return_value=True), "transfer"
    log Recovered(token=token, amount=amount)
