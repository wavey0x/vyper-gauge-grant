# pragma version ==0.4.3
interface Token:
    def transferFrom(owner: address, to: address, amount: uint256) -> bool: nonpayable
    def mint(account: address, amount: uint256): nonpayable

coins: public(address[2])
execution_bps: public(uint256)

@deploy
def __init__(usdc: address, crvusd: address):
    self.coins = [usdc, crvusd]
    self.execution_bps = 10000

@external
def configure(execution_bps: uint256):
    self.execution_bps = execution_bps

@external
@view
def get_dy(i: int128, j: int128, amount: uint256) -> uint256:
    assert i == 1 and j == 0
    return amount // 10**12

@external
def exchange(i: int128, j: int128, amount: uint256, min_out: uint256) -> uint256:
    assert i == 1 and j == 0
    output: uint256 = amount // 10**12 * self.execution_bps // 10000
    assert output >= min_out, "slippage"
    assert extcall Token(self.coins[1]).transferFrom(msg.sender, self, amount)
    extcall Token(self.coins[0]).mint(msg.sender, output)
    return output
