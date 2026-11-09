import boa
from conftest import WEEK, UNIT, advance


def begin(s, weeks=1, sync=True):
    with boa.env.prank(s.manager):
        for i in range(weeks):
            s.funding.purchase(s.start + i * WEEK, 1000 * 10**6)
    advance(s.start + 3600)
    if sync:
        s.escrow.sync()


def preview(s):
    with boa.env.anchor():
        return s.escrow.sync()


def sale(s, amount=None):
    amount = preview(s) if amount is None else amount
    quote = s.escrow.quote(amount)
    with boa.env.prank(s.keeper):
        s.escrow.sell(amount, quote * 99 // 100)
    return amount, quote


def test_budget_is_assets_not_shares_or_yield(system):
    s = system
    s.usdc.mint(s.vault.address, 10_000 * 10**6)
    s.vault.configure(11 * UNIT // 10, False)
    assert s.funding.assets() == 110_000 * 10**6
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 60_000 * 10**6)
        s.funding.purchase(s.start + WEEK, 40_000 * 10**6)
        with boa.reverts("budget"):
            s.funding.purchase(s.start + 2 * WEEK, 1)
        s.funding.recover(s.vault.address, s.vault.balanceOf(s.funding.address))
    assert s.funding.spent() == 100_000 * 10**6
    assert s.funding.remaining() == 0
    assert s.usdc.balanceOf(s.payee) == 100_000 * 10**6


def test_failed_withdrawal_does_not_spend_budget(system):
    s = system
    s.vault.configure(UNIT, True)
    with boa.env.prank(s.manager), boa.reverts("withdrawal"):
        s.funding.purchase(s.start, 100 * 10**6)
    assert s.funding.spent() == s.funding.paid(s.start) == 0


def test_purchase_permissions_and_unique_future_weeks(system):
    s = system
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.funding.purchase(s.start, 1)
    with boa.env.prank(s.manager):
        for week in [s.start + 1, s.start - WEEK, s.start + 8 * WEEK]:
            with boa.reverts("week"):
                s.funding.purchase(week, 1)
        s.funding.purchase(s.start, 1)
        with boa.reverts("future unpaid week"):
            s.funding.purchase(s.start, 1)
        advance(s.start + WEEK)
        with boa.reverts("future unpaid week"):
            s.funding.purchase(s.start + WEEK, 1)


def test_first_sale_syncs_without_preliminary_transaction(system):
    s = system
    baseline, cursor = s.escrow.accrued(), s.escrow.cursor()
    s.crv.mint(s.receiver, 900 * UNIT)
    s.usd.mint(s.escrow.address, 500 * UNIT)
    begin(s, sync=False)
    assert s.escrow.available() == 0 and s.escrow.cursor() == cursor
    amount, output = sale(s)
    assert s.escrow.cursor() == boa.env.evm.patch.timestamp
    assert amount <= s.escrow.accrued() - baseline
    assert s.escrow.available() == s.escrow.weeks(s.start).remaining == 0
    assert s.usd.balanceOf(s.escrow.address) == 500 * UNIT + output // 4
    assert s.usd.balanceOf(s.receiver) == output - output // 4
    assert s.crv.balanceOf(s.receiver) >= 900 * UNIT


def test_public_sync_captures_exact_boundary_and_works_while_paused(system):
    s = system
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 1)
        s.escrow.set_paused(True)
    advance(s.start)
    with boa.env.prank(s.stranger):
        assert s.escrow.sync() == 0
        row = s.escrow.weeks(s.start)
        assert row.ready and row.yearn * 4 == row.total
        assert s.escrow.sync() == 0
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    advance(s.start + 3600)
    with boa.env.prank(s.stranger):
        assert s.escrow.sync() > 0
    assert s.escrow.weeks(s.start).yearn == row.yearn
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.sell(1, 0)


def test_one_swap_consumes_two_weeks_with_different_ratios(system):
    s = system
    begin(s, 2)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start + 3600)
    advance(s.start + WEEK + 3600)
    with boa.env.anchor():
        amount = s.escrow.sync()
        first = s.escrow.weeks(s.start).remaining
        second = s.escrow.weeks(s.start + WEEK).remaining
    output = s.escrow.quote(amount)
    first_output = output * first // amount
    expected = first_output // 4 + (output - first_output) // 2
    sale(s, amount)
    assert first + second == amount
    assert s.usd.balanceOf(s.escrow.address) == expected
    assert s.usd.balanceOf(s.receiver) == output - expected
    assert s.escrow.available() == 0
    assert (
        s.escrow.weeks(s.start).remaining
        == s.escrow.weeks(s.start + WEEK).remaining
        == 0
    )
    assert s.escrow.sale_week() == s.start + WEEK


def test_tiny_old_remainder_is_consumed_with_new_week(system):
    s = system
    begin(s, 2)
    advance(s.start + WEEK)
    s.escrow.sync()
    first = s.escrow.weeks(s.start).remaining
    sale(s, first - 1)
    assert s.escrow.weeks(s.start).remaining == 1
    advance(s.start + WEEK + 3600)
    sale(s)
    assert s.escrow.weeks(s.start).remaining == 0
    assert s.escrow.available() == 0


def test_missing_weight_recovery_is_bounded_and_once_only(system):
    s = system
    begin(s, sync=False)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    with boa.reverts("missing vote history"):
        s.escrow.sync()
    correct = 10**12 * 52 * WEEK
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.escrow.set_missing_weight(s.start, correct)
    with boa.env.prank(s.manager):
        with boa.reverts("weight"):
            s.escrow.set_missing_weight(s.start, correct * 5)
        s.escrow.set_missing_weight(s.start, correct)
        with boa.reverts("already captured"):
            s.escrow.set_missing_weight(s.start, 0)
    sale(s)
    assert s.escrow.weeks(s.start).yearn == correct


def test_failed_sale_rolls_back_sync_claim_and_all_transfers(system):
    s = system
    begin(s, sync=False)
    cursor, accrued = s.escrow.cursor(), s.escrow.accrued()
    amount = preview(s)
    quote = s.escrow.quote(amount)
    with boa.env.prank(s.stranger), boa.reverts("keeper or paused"):
        s.escrow.sell(amount, quote)
    with boa.env.prank(s.keeper):
        with boa.reverts("sale amount"):
            s.escrow.sell(amount + 1, 0)
        with boa.reverts("slippage"):
            s.escrow.sell(amount, quote + 1)
    assert s.escrow.cursor() == cursor and s.escrow.accrued() == accrued
    assert not s.escrow.weeks(s.start).ready
    assert s.escrow.available() == 0
    assert s.crv.balanceOf(s.receiver) == s.usd.balanceOf(s.escrow.address) == 0


def test_operator_minimum_is_the_only_price_policy(system):
    s = system
    begin(s)
    amount = s.escrow.available()
    # Deliberately large price move: operator's explicit minimum governs execution.
    s.pool.configure(UNIT // 20, 10000)
    output = s.escrow.quote(amount)
    with boa.env.prank(s.keeper):
        s.escrow.sell(amount, output)
    assert s.usd.balanceOf(s.escrow.address) == output // 4


def test_no_funded_weeks_means_no_entitlement(system):
    s = system
    advance(s.start + WEEK + 100)
    assert s.escrow.sync() == s.escrow.available() == 0


def test_manager_recovers_any_token_while_paused(system):
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
