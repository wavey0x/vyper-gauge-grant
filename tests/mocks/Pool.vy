# pragma version ==0.4.3
interface Token:
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable
    def mint(account: address, amount: uint256): nonpayable

coins: public(address[3])
price: uint256
execution_bps: uint256

@deploy
def __init__(crv: address, crvusd: address):
    self.coins[2] = crv
    self.coins[0] = crvusd
    self.price = 10**18 // 4
    self.execution_bps = 10000

@external
def configure(price: uint256, execution_bps: uint256):
    self.price = price
    self.execution_bps = execution_bps

@external
@view
def price_oracle(index: uint256) -> uint256:
    assert index == 1
    return self.price

@external
@view
def get_dy(i: uint256, j: uint256, amount: uint256) -> uint256:
    assert i == 2 and j == 0
    return amount * self.price // 10**18

@external
def exchange(i: uint256, j: uint256, amount: uint256, min_out: uint256) -> uint256:
    assert i == 2 and j == 0
    output: uint256 = amount * self.price // 10**18 * self.execution_bps // 10000
    assert output >= min_out, "slippage"
    assert extcall Token(self.coins[2]).transferFrom(msg.sender, self, amount)
    extcall Token(self.coins[0]).mint(msg.sender, output)
    return output
