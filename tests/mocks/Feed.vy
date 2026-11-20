# pragma version ==0.4.3
answer: public(int256)
updated: public(uint256)

@deploy
def __init__():
    self.answer = 25_000_000

@external
def set(answer: int256, updated: uint256):
    self.answer = answer
    self.updated = updated

@external
@view
def latestRoundData() -> (uint80, int256, uint256, uint256, uint80):
    # Zero means "updated now", so tests that travel in time keep a fresh price.
    t: uint256 = self.updated if self.updated > 0 else block.timestamp
    return (1, self.answer, t, t, 1)

@external
@view
def decimals() -> uint8:
    return 8
