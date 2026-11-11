# pragma version ==0.4.3
"""Hold yvUSDC-1 and let Yearn draw USDC to its chosen recipient."""

from ethereum.ercs import IERC20

interface Vault:
    def convertToAssets(shares: uint256) -> uint256: view
    def withdraw(assets: uint256, receiver: address, owner: address, max_loss: uint256) -> uint256: nonpayable

event Drawn:
    usdc: uint256
    recipient: address

event RecipientSet:
    recipient: address

event Recovered:
    token: indexed(address)
    amount: uint256
    receiver: address

VAULT: public(constant(address)) = 0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204
MANAGER: public(constant(address)) = 0x4444AAAACDBa5580282365e25b16309Bd770ce4a
GOVERNANCE: public(constant(address)) = 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52
recipient: public(address)

@deploy
def __init__():
    self.recipient = 0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8

@external
@view
def assets() -> uint256:
    return staticcall Vault(VAULT).convertToAssets(staticcall IERC20(VAULT).balanceOf(self))

@external
@nonreentrant
def draw(amount: uint256):
    assert msg.sender == MANAGER, "manager"
    assert amount > 0, "amount"
    recipient: address = self.recipient
    extcall Vault(VAULT).withdraw(amount, recipient, self, 0)
    log Drawn(usdc=amount, recipient=recipient)

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
    assert extcall IERC20(token).transfer(msg.sender, amount, default_return_value=True), "transfer"
    log Recovered(token=token, amount=amount, receiver=msg.sender)
