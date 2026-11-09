# pragma version ==0.4.3
balanceOf: public(HashMap[address, uint256])
allowance: public(HashMap[address, HashMap[address, uint256]])
rejected: address
@external
def reject(account: address):
    self.rejected = account
@external
def mint(account: address, amount: uint256):
    self.balanceOf[account] += amount
@external
def approve(spender: address, amount: uint256) -> bool:
    self.allowance[msg.sender][spender] = amount
    return True
@external
def transfer(to: address, amount: uint256) -> bool:
    assert to != self.rejected, "recipient rejected"
    self.balanceOf[msg.sender] -= amount
    self.balanceOf[to] += amount
    return True
@external
def transferFrom(owner: address, to: address, amount: uint256) -> bool:
    self.allowance[owner][msg.sender] -= amount
    self.balanceOf[owner] -= amount
    self.balanceOf[to] += amount
    return True
