import boa
import pytest
from conftest import WEEK, UNIT, advance


def begin(s, weeks=1):
    with boa.env.prank(s.manager):
        for i in range(weeks):
            s.funding.purchase(s.start + i * WEEK, 1000 * 10**6)
    # Clear historical rewards just before the first funded week.
    advance(s.start - 60)
    sale(s)
    advance(s.start + 3600)


def preview(s):
    with boa.env.anchor():
        return s.escrow.quote()


def sale(s):
    amount, output = preview(s)
    with boa.env.prank(s.keeper):
        s.escrow.sell(output * 99 // 100)
    return amount, output


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


def test_full_claim_autocaptures_and_preserves_existing_balances(system):
    s = system
    begin(s)
    s.crv.mint(s.receiver, 900 * UNIT)
    s.crv.mint(s.escrow.address, 7 * UNIT)
    s.usd.mint(s.escrow.address, 500 * UNIT)
    before_minted = s.minter.minted(s.receiver, s.gauge.address)
    before_returned = s.usd.balanceOf(s.receiver)
    assert not s.escrow.weeks(s.start).ready
    amount, output = sale(s)
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.minter.minted(s.receiver, s.gauge.address) - before_minted == amount
    assert s.gauge.integrate_fraction(s.receiver) == s.minter.minted(
        s.receiver, s.gauge.address
    )
    assert s.crv.balanceOf(s.receiver) == 900 * UNIT
    assert s.crv.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.usd.balanceOf(s.escrow.address) == 500 * UNIT + output // 4
    assert s.usd.balanceOf(s.receiver) - before_returned == output - output // 4
    assert preview(s) == (0, 0)
    with boa.env.prank(s.keeper), boa.reverts("nothing minted"):
        s.escrow.sell(0)


def test_public_checkpoint_captures_boundary_without_claiming_while_paused(system):
    s = system
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 1)
        s.escrow.set_paused(True)
    advance(s.start)
    with boa.env.prank(s.stranger):
        assert s.escrow.checkpoint() == UNIT // 4
    assert s.minter.minted(s.receiver, s.gauge.address) == 0
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    advance(s.start + 3600)
    assert s.escrow.checkpoint() == UNIT // 4
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.sell(0)


def test_boundary_tail_uses_new_week_share(system):
    s = system
    begin(s, 2)
    sale(s)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start + 3600)
    advance(s.start + WEEK - 180)
    sale(s)
    advance(s.start + WEEK + 60)
    before = s.usd.balanceOf(s.escrow.address)
    amount, output = sale(s)
    sold = s.escrow.get_logs()[-1]
    assert amount > 0
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.escrow.weeks(s.start + WEEK).share == UNIT // 2
    assert s.usd.balanceOf(s.escrow.address) - before == output // 2
    assert sold.week == s.start + WEEK and sold.crv == amount


@pytest.mark.parametrize("when", ["before", "unfunded", "after"])
def test_unfunded_claims_return_everything_to_vyper(system, when):
    s = system
    begin(s)
    retained = s.usd.balanceOf(s.escrow.address)
    if when == "before":
        # begin() already settled a real pre-programme claim.
        assert retained == 0 and s.usd.balanceOf(s.receiver) > 0
        return
    advance(s.start + WEEK + 100 if when == "unfunded" else s.funding.END() + 100)
    before = s.usd.balanceOf(s.receiver)
    _, output = sale(s)
    assert s.usd.balanceOf(s.receiver) - before == output
    assert s.usd.balanceOf(s.escrow.address) == retained


def test_missing_weight_is_bounded_and_once_only(system):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    with boa.reverts("missing vote history"):
        s.escrow.checkpoint()
    correct = 10**12 * 52 * WEEK
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.escrow.set_missing_weight(s.start, correct)
    with boa.env.prank(s.manager):
        with boa.reverts("weight"):
            s.escrow.set_missing_weight(s.start, correct * 5)
        s.escrow.set_missing_weight(s.start, correct)
        with boa.reverts("already captured"):
            s.escrow.set_missing_weight(s.start, 0)
    assert s.escrow.checkpoint() == UNIT // 4
    sale(s)


@pytest.mark.parametrize("failure", ["mint", "allowance", "slippage", "return"])
def test_failed_sale_rolls_back_snapshot_claim_and_transfers(system, failure):
    s = system
    begin(s)
    if failure == "mint":
        with boa.env.prank(s.receiver):
            s.minter.toggle_approve_mint(s.escrow.address)
    elif failure == "allowance":
        with boa.env.prank(s.receiver):
            s.crv.approve(s.escrow.address, 0)
    elif failure == "return":
        s.usd.reject(s.receiver)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    accrued = s.gauge.integrate_fraction(s.receiver)
    balances = (s.crv.balanceOf(s.receiver), s.usd.balanceOf(s.receiver))
    _, output = preview(s)
    with boa.env.prank(s.keeper), boa.reverts():
        s.escrow.sell(output + 1 if failure == "slippage" else 0)
    assert not s.escrow.weeks(s.start).ready
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.gauge.integrate_fraction(s.receiver) == accrued
    assert (s.crv.balanceOf(s.receiver), s.usd.balanceOf(s.receiver)) == balances
    assert s.crv.balanceOf(s.escrow.address) == s.usd.balanceOf(s.escrow.address) == 0


@pytest.mark.parametrize("share", [0, 1, 3, 4])
def test_split_extremes_and_rounding_conserve_output(system, share):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, share * 10**12, s.start + 52 * WEEK, s.start - 1)
    before = s.usd.balanceOf(s.receiver)
    _, output = sale(s)
    retained = output * share // 4
    assert s.usd.balanceOf(s.escrow.address) == retained
    assert s.usd.balanceOf(s.receiver) - before == output - retained


def test_zero_weight_week_can_settle_pending_tail(system):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, 0, s.start, s.start - 1)
    s.gauge.set_weight(s.start, 0, 0)
    assert s.escrow.checkpoint() == 0
    _, output = sale(s)  # The unclaimed minute before START still exists.
    assert output > 0 and s.usd.balanceOf(s.escrow.address) == 0


def test_gauge_owns_rate_changes_and_cap_exhaustion(system):
    s = system
    begin(s)
    s.gauge.set_params(
        s.gauge.max_emissions(), s.gauge.inflation_rate(), s.start + 7200, False
    )
    sale(s)
    advance(s.start + 8000)
    sale(s)
    assert s.gauge.future_epoch_time() > s.start + 7200
    # A capped gauge may already report killed; its final claim remains saleable.
    cap = s.gauge.integrate_fraction(s.receiver) + UNIT
    s.gauge.set_params(
        cap, s.gauge.inflation_rate(), s.gauge.future_epoch_time(), False
    )
    advance(s.start + 9000)
    s.gauge.user_checkpoint(s.receiver)
    assert s.gauge.is_killed()
    amount, _ = sale(s)
    assert amount == UNIT and preview(s) == (0, 0)


def test_operator_minimum_is_the_only_price_policy(system):
    s = system
    begin(s)
    s.pool.configure(UNIT // 20, 10000)
    _, output = sale(s)
    assert s.usd.balanceOf(s.escrow.address) == output // 4


def test_other_claimers_leave_existing_crv_outside_sales(system):
    s = system
    begin(s)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(s.stranger)
    with boa.env.prank(s.stranger):
        s.minter.mint_for(s.gauge.address, s.receiver)
    old = s.crv.balanceOf(s.receiver)
    assert old > 0 and preview(s) == (0, 0)
    advance(s.start + 7200)
    sale(s)
    assert s.crv.balanceOf(s.receiver) == old


def test_only_authorized_keepers_sell(system):
    s = system
    begin(s)
    with boa.env.prank(s.stranger), boa.reverts("keeper or paused"):
        s.escrow.sell(0)
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.keeper, False)
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.sell(0)


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
