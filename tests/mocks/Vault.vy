# pragma version ==0.4.3
interface Token:
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable
    def transfer(to: address, amount: uint256) -> bool: nonpayable

asset: public(address)
balanceOf: public(HashMap[address, uint256])
price: public(uint256)
blocked: bool

@deploy
def __init__(asset: address):
    self.asset = asset
    self.price = 10**18

@external
def configure(price: uint256, blocked: bool):
    self.price = price
    self.blocked = blocked

@external
@view
def convertToAssets(shares: uint256) -> uint256:
    return shares * self.price // 10**18

@external
def deposit(amount: uint256, receiver: address) -> uint256:
    assert extcall Token(self.asset).transferFrom(msg.sender, self, amount)
    shares: uint256 = amount * 10**18 // self.price
    self.balanceOf[receiver] += shares
    return shares

@external
def transfer(to: address, shares: uint256) -> bool:
    self.balanceOf[msg.sender] -= shares
    self.balanceOf[to] += shares
    return True

@external
def withdraw(assets: uint256, receiver: address, owner: address, max_loss: uint256) -> uint256:
    assert msg.sender == owner and max_loss == 0 and not self.blocked, "withdrawal"
    shares: uint256 = (assets * 10**18 + self.price - 1) // self.price
    self.balanceOf[owner] -= shares
    assert extcall Token(self.asset).transfer(receiver, assets)
    return shares
