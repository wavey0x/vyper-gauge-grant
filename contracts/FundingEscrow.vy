# pragma version ==0.4.3
"""@title Hold yvUSDC-1 and fund the next two UTC voting weeks."""

from ethereum.ercs import IERC20

interface Vault:
    def convertToAssets(shares: uint256) -> uint256: view
    def withdraw(assets: uint256, receiver: address, owner: address, max_loss: uint256) -> uint256: nonpayable

event Drawn:
    usdc: uint256
    recipient: address

event RoundFunded:
    usdc: uint256
    first_week: uint256

event Cancelled:
    usdc: uint256

event RecipientSet:
    recipient: address

event Recovered:
    token: indexed(address)
    amount: uint256
    receiver: address

WEEK: constant(uint256) = 604800
VAULT: public(constant(address)) = 0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
GOVERNANCE: public(constant(address)) = 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52
recipient: public(address)
payments: public(uint256[2])
next_week: public(uint256)

@deploy
def __init__():
    """@notice Use Yearn's fee burner as the initial payment recipient."""
    self.recipient = 0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8

@external
@view
def assets() -> uint256:
    """@return USDC value of held vault shares, in 6-decimal units."""
    return staticcall Vault(VAULT).convertToAssets(staticcall IERC20(VAULT).balanceOf(self))

@external
def fund_round(amount: uint256):
    """
    @notice Queue equal payments at the next two Thursday 00:00 UTC boundaries.
    @param amount Total USDC allocation in 6-decimal units; each payment is amount // 2.
    """
    assert msg.sender == MANAGER, "manager"
    assert amount >= 2, "amount"
    assert self.next_week == 0, "pending round"
    self.payments = [amount // 2, amount // 2]
    self.next_week = (block.timestamp // WEEK + 1) * WEEK
    log RoundFunded(usdc=amount, first_week=self.next_week)

@external
@view
def canWork() -> bool:
    """@return Whether a scheduled payment is due, without checking vault liquidity."""
    return self.next_week > 0 and block.timestamp >= self.next_week

@external
@nonreentrant
def work():
    """@notice Release one due payment from yvUSDC-1 to the configured recipient."""
    assert self.next_week > 0 and block.timestamp >= self.next_week, "not due"
    amount: uint256 = self.payments[0]
    self.payments = [self.payments[1], 0]
    self.next_week = self.next_week + WEEK if self.payments[0] > 0 else 0
    recipient: address = self.recipient
    extcall Vault(VAULT).withdraw(amount, recipient, self, 0)
    log Drawn(usdc=amount, recipient=recipient)

@internal
def _cancel():
    log Cancelled(usdc=self.payments[0] + self.payments[1])
    self.payments = empty(uint256[2])
    self.next_week = 0

@external
def cancel():
    """@notice Let the manager or governance cancel unpaid instalments, keeping funds here."""
    assert msg.sender == MANAGER or msg.sender == GOVERNANCE, "management"
    self._cancel()

@external
def set_recipient(recipient: address):
    """@param recipient Nonzero destination for unpaid instalments, other than this escrow."""
    assert msg.sender == MANAGER, "manager"
    assert recipient != empty(address) and recipient != self, "recipient"
    self.recipient = recipient
    log RecipientSet(recipient=recipient)

@external
@nonreentrant
def recover(token: address, amount: uint256):
    """
    @notice Return tokens to the manager or governance caller; vault recovery cancels payments.
    @param token ERC20 token to recover.
    @param amount Amount in the token's smallest units.
    """
    assert msg.sender == MANAGER or msg.sender == GOVERNANCE, "management"
    if token == VAULT and self.next_week > 0:
        self._cancel()
    assert extcall IERC20(token).transfer(msg.sender, amount, default_return_value=True), "transfer"
    log Recovered(token=token, amount=amount, receiver=msg.sender)
