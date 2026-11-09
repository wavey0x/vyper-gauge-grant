import boa
from conftest import WEEK, UNIT, advance

REF = b"purchase".ljust(32, b"\0")


def begin(s, weeks=1):
    with boa.env.prank(s.manager):
        for i in range(weeks):
            s.funding.purchase(s.start + i * WEEK, 1000 * 10**6, REF)
        s.escrow.activate()
    advance(s.start + 3600)
    s.escrow.checkpoint()


def sale(s, amount=None):
    week, available = s.escrow.next_sale()
    amount = available if amount is None else amount
    quote = s.escrow.quote(amount)
    with boa.env.prank(s.keeper):
        s.escrow.sell(
            week, amount, quote * 99 // 100, boa.env.evm.patch.timestamp + 120
        )
    return amount, quote


def test_budget_is_assets_not_shares_or_yield(system):
    s = system
    s.usdc.mint(s.vault.address, 10_000 * 10**6)
    s.vault.configure(11 * UNIT // 10, False)
    assert s.funding.assets() == 110_000 * 10**6
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 60_000 * 10**6, REF)
        s.funding.purchase(s.start + WEEK, 40_000 * 10**6, REF)
        with boa.reverts("budget"):
            s.funding.purchase(s.start + 2 * WEEK, 1, REF)
    assert s.funding.spent() == 100_000 * 10**6
    assert s.funding.remaining() == 0
    assert s.usdc.balanceOf(s.payee) == 100_000 * 10**6
    assert s.funding.assets() >= 9999 * 10**6


def test_failed_withdrawal_does_not_spend_budget(system):
    s = system
    s.vault.configure(UNIT, True)
    with boa.env.prank(s.manager), boa.reverts("withdrawal"):
        s.funding.purchase(s.start, 100 * 10**6, REF)
    assert s.funding.spent() == s.funding.paid(s.start) == 0


def test_purchase_permissions_and_unique_future_weeks(system):
    s = system
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.funding.purchase(s.start, 1, REF)
    with boa.env.prank(s.manager):
        for week in [s.start + 1, s.start - WEEK, s.start + 8 * WEEK]:
            with boa.reverts("week"):
                s.funding.purchase(week, 1, REF)
        s.funding.purchase(s.start, 1, REF)
        with boa.reverts("future unpaid week"):
            s.funding.purchase(s.start, 1, REF)
        advance(s.start + WEEK)
        with boa.reverts("future unpaid week"):
            s.funding.purchase(s.start + WEEK, 1, REF)


def test_atomic_split_excludes_old_wallet_and_escrow_balances(system):
    s = system
    s.crv.mint(s.receiver, 900 * UNIT)
    s.usd.mint(s.escrow.address, 500 * UNIT)
    begin(s)
    old = s.usd.balanceOf(s.escrow.address)
    amount, quote = sale(s)
    assert s.usd.balanceOf(s.receiver) == quote - quote // 4
    assert s.usd.balanceOf(s.escrow.address) == old + quote // 4
    assert s.escrow.total_retained() == quote // 4
    assert s.escrow.total_sold() == amount
    assert s.crv.balanceOf(s.receiver) >= 900 * UNIT
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
        s.escrow.recover(s.usd.address, old + quote // 4)
    assert s.escrow.total_retained() == quote // 4


def test_midweek_revote_preserves_frozen_ratio(system):
    s = system
    begin(s, 2)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start + 3600)
    advance(s.start + 7200)
    s.escrow.checkpoint()
    assert s.escrow.weeks(s.start).yearn * 4 == s.escrow.weeks(s.start).total
    advance(s.start + WEEK + 3600)
    s.escrow.checkpoint()
    assert (
        s.escrow.weeks(s.start + WEEK).yearn * 2 == s.escrow.weeks(s.start + WEEK).total
    )


def test_missing_vote_never_becomes_zero_and_repair_is_immutable(system):
    s = system
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 1000 * 10**6, REF)
        s.escrow.activate()
    advance(s.start + 1)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    with boa.reverts("vote history"):
        s.escrow.checkpoint()
    assert not s.escrow.weeks(s.start).ready
    with boa.env.prank(s.stranger), boa.reverts("manager evidence"):
        s.escrow.repair_week(s.start, 10**12, s.start + 52 * WEEK, s.start - 1, REF)
    with boa.env.prank(s.manager):
        s.escrow.repair_week(s.start, 10**12, s.start + 52 * WEEK, s.start - 1, REF)
        with boa.reverts("vote history"):
            s.escrow.repair_week(s.start, 0, 0, 0, REF)
    s.escrow.checkpoint()
    assert s.escrow.weeks(s.start).yearn > 0


def test_sale_access_and_failures_are_atomic(system):
    s = system
    begin(s)
    week, amount = s.escrow.next_sale()
    quote = s.escrow.quote(amount)
    now = boa.env.evm.patch.timestamp
    minted = s.gauge.minted(s.receiver, s.gauge.address)
    with boa.env.prank(s.stranger), boa.reverts("keeper or paused"):
        s.escrow.sell(week, amount, quote, now)
    with boa.env.prank(s.keeper):
        with boa.reverts("price guard"):
            s.escrow.sell(week, amount, 1, now)
        with boa.reverts("sale amount"):
            s.escrow.sell(week + WEEK, amount, quote, now)
        with boa.reverts("timing"):
            s.escrow.sell(week, amount, quote, now - 1)
        s.pool.configure(UNIT // 4, 9500)
        with boa.reverts("slippage"):
            s.escrow.sell(week, amount, quote * 99 // 100, now)
    assert s.escrow.total_sold() == s.escrow.total_output() == 0
    assert s.gauge.minted(s.receiver, s.gauge.address) == minted


def test_output_rounding_is_cumulative(system):
    s = system
    begin(s)
    for amount in [401, 405, 409]:
        sale(s, amount)
    row = s.escrow.weeks(s.start)
    assert row.retained == row.output // 4
    assert s.usd.balanceOf(s.receiver) + s.usd.balanceOf(s.escrow.address) == row.output


def test_no_funded_weeks_means_no_entitlement(system):
    s = system
    with boa.env.prank(s.manager):
        s.escrow.activate()
    advance(s.start + WEEK + 100)
    assert s.escrow.checkpoint() == 0
    assert s.escrow.next_sale() == (0, 0)


def test_manager_can_recover_nonreturning_token_but_keeper_cannot(system):
    s = system
    token = boa.loads("""
# pragma version ==0.4.3
balanceOf: public(HashMap[address, uint256])
@external
def mint(to: address, amount: uint256):
    self.balanceOf[to] += amount
@external
def transfer(to: address, amount: uint256):
    self.balanceOf[msg.sender] -= amount
    self.balanceOf[to] += amount
""")
    token.mint(s.escrow.address, 123)
    with boa.env.prank(s.keeper), boa.reverts("manager"):
        s.escrow.recover(token.address, 123)
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
        s.escrow.recover(token.address, 123)
    assert token.balanceOf(s.manager) == 123


def test_unquotable_crv_dust_cannot_block_later_weeks(system):
    s = system
    begin(s, 2)
    amount = s.escrow.next_sale()[1]
    sale(s, amount - 1)
    assert s.escrow.next_sale() == (0, 0)
    advance(s.start + WEEK + 3600)
    s.escrow.checkpoint()
    # Finish the now-closed first week, deliberately leaving one wei.
    while s.escrow.next_sale()[0] == s.start:
        amount = s.escrow.next_sale()[1]
        outstanding = s.escrow.weeks(s.start).earned - s.escrow.weeks(s.start).sold
        sale(s, amount - 1 if amount == outstanding else amount)
    assert s.escrow.next_sale()[0] == s.start + WEEK
    assert s.escrow.weeks(s.start).earned - s.escrow.weeks(s.start).sold == 1
