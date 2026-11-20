# pragma version 0.4.3
"""
@title Keeper guard for ProceedsEscrow
@notice Sell-only forwarder that floors each sale at 97.5% of Chainlink's CRV/USD value.
@dev Holds no funds and has no owner. To change anything, deploy a new guard and have
     the manager disable the old one and enable the new one in one Safe batch.
"""

interface Escrow:
    def work(min_out: uint256, max_crv: uint256, sell_vyper: bool): nonpayable

interface Feed:
    def latestRoundData() -> (uint80, int256, uint256, uint256, uint80): view

FEED: constant(address) = 0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f  # Chainlink CRV/USD, 8 decimals
MAX_AGE: constant(uint256) = 90000  # 25 hours, against the feed's 24-hour heartbeat
TOL_BPS: constant(uint256) = 250

ESCROW: public(immutable(address))
BOT: public(immutable(address))


@deploy
def __init__(escrow: address, bot: address):
    assert escrow != empty(address) and bot != empty(address), "address"
    ESCROW = escrow
    BOT = bot


@external
def work(min_out: uint256, max_crv: uint256):
    """
    @notice Sell up to max_crv of Vyper's CRV through the escrow, never below the oracle floor.
    @param min_out The bot's own USDC minimum; the larger of it and the floor applies.
    @param max_crv Maximum CRV to pull, in 18-decimal units.
    """
    assert msg.sender == BOT, "bot"
    round_id: uint80 = 0
    answer: int256 = 0
    started: uint256 = 0
    updated: uint256 = 0
    answered: uint80 = 0
    round_id, answer, started, updated, answered = staticcall Feed(FEED).latestRoundData()
    # Checked arithmetic reverts on a negative answer, a future timestamp and overflow.
    assert block.timestamp - updated <= MAX_AGE, "stale"
    # 18-decimal CRV times the 8-decimal price, scaled to 6-decimal USDC.
    oracle_min: uint256 = max_crv * convert(answer, uint256) * (10000 - TOL_BPS) // (10000 * 10**20)
    assert oracle_min > 0, "floor"
    extcall Escrow(ESCROW).work(max(min_out, oracle_min), max_crv, True)
