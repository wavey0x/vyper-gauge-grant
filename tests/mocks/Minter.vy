# pragma version ==0.4.3
interface Gauge:
    def user_checkpoint(account: address) -> bool: nonpayable
    def integrate_fraction(account: address) -> uint256: view
interface Token:
    def mint(account: address, amount: uint256): nonpayable

token: immutable(address)
minted: public(HashMap[address, HashMap[address, uint256]])
allowed_to_mint_for: public(HashMap[address, HashMap[address, bool]])

@deploy
def __init__(crv: address):
    token = crv

@external
def toggle_approve_mint(delegate: address):
    self.allowed_to_mint_for[delegate][msg.sender] = not self.allowed_to_mint_for[delegate][msg.sender]

@external
def mint_for(gauge: address, account: address):
    if self.allowed_to_mint_for[msg.sender][account]:
        assert extcall Gauge(gauge).user_checkpoint(account)
        amount: uint256 = staticcall Gauge(gauge).integrate_fraction(account) - self.minted[account][gauge]
        self.minted[account][gauge] += amount
        extcall Token(token).mint(account, amount)
