# pragma version ==0.4.3
"""Draw USDC from a fixed budget for the upcoming vote week."""

interface Token:
    def balanceOf(account: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable

interface Vault:
    def convertToAssets(shares: uint256) -> uint256: view
    def withdraw(assets: uint256, receiver: address, owner: address, max_loss: uint256) -> uint256: nonpayable

event Drawn:
    week: indexed(uint256)
    usdc: uint256
    recipient: address

event RecipientSet:
    recipient: address

event Recovered:
    token: indexed(address)
    amount: uint256
    receiver: address

WEEK: constant(uint256) = 604800
BUDGET: public(constant(uint256)) = 100_000 * 10**6
VAULT: public(constant(address)) = 0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204
USDC: public(constant(address)) = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
GOVERNANCE: public(constant(address)) = 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52
START: public(immutable(uint256))
END: public(immutable(uint256))
recipient: public(address)
spent: public(uint256)
paid: public(HashMap[uint256, uint256])

@deploy
def __init__(start: uint256, weeks: uint256):
    assert start > block.timestamp and start % WEEK == 0, "start"
    assert weeks > 0 and weeks <= 52, "duration"
    self.recipient = 0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8
    START = start
    END = start + weeks * WEEK

@external
@view
def assets() -> uint256:
    return staticcall Token(USDC).balanceOf(self) + staticcall Vault(VAULT).convertToAssets(staticcall Token(VAULT).balanceOf(self))

@external
@view
def remaining() -> uint256:
    return BUDGET - self.spent

@external
@nonreentrant
def draw(amount: uint256):
    assert msg.sender == MANAGER, "manager"
    week: uint256 = (block.timestamp // WEEK + 1) * WEEK
    assert week >= START and week < END, "week"
    assert amount > 0 and self.spent + amount <= BUDGET, "budget"
    self.paid[week] += amount
    self.spent += amount
    recipient: address = self.recipient
    before: uint256 = staticcall Token(USDC).balanceOf(recipient)
    liquid: uint256 = min(amount, staticcall Token(USDC).balanceOf(self))
    if liquid > 0:
        assert extcall Token(USDC).transfer(recipient, liquid), "transfer"
    if liquid < amount:
        extcall Vault(VAULT).withdraw(amount - liquid, recipient, self, 0)
    assert staticcall Token(USDC).balanceOf(recipient) - before == amount, "payment"
    log Drawn(week=week, usdc=amount, recipient=recipient)

@external
def set_recipient(recipient: address):
    assert msg.sender == MANAGER, "manager"
    assert recipient != empty(address) and recipient != self, "recipient"
    self.recipient = recipient
    log RecipientSet(recipient=recipient)

@external
@nonreentrant
def recover(token: address, amount: uint256):
    assert msg.sender == MANAGER or msg.sender == GOVERNANCE, "management"
    assert extcall Token(token).transfer(msg.sender, amount, default_return_value=True), "transfer"
    log Recovered(token=token, amount=amount, receiver=msg.sender)
