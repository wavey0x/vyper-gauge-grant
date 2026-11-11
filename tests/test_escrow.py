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
        amount = s.escrow.claimable()
        return amount, s.escrow.quote(amount)


def sale(s):
    amount, output = preview(s)
    with boa.env.prank(s.keeper):
        s.escrow.sell(
            output * 99 // 100,
            boa.env.evm.patch.timestamp // WEEK * WEEK,
            amount,
            amount,
        )
    return amount, output


def test_claimable_checkpoints_without_minting_and_quote_only_prices(system):
    s = system
    begin(s)
    last = s.gauge.last_checkpoint()
    minted = s.minter.minted(s.receiver, s.gauge.address)
    balance = s.crv.balanceOf(s.receiver)
    assert s.escrow.quote(0) == 0
    assert s.escrow.quote(10 * UNIT) == s.stable_pool.get_dy(
        1, 0, s.pool.get_dy(2, 0, 10 * UNIT)
    )
    assert s.gauge.last_checkpoint() == last
    amount = s.escrow.claimable()
    assert amount > 0 and s.gauge.last_checkpoint() == boa.env.evm.patch.timestamp
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.crv.balanceOf(s.receiver) == balance
    assert not s.escrow.weeks(s.start).ready
    assert s.escrow.claimable() == amount


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


def test_funding_spends_liquid_usdc_then_vault_shares(system):
    s = system
    s.usdc.mint(s.funding.address, 100 * 10**6)
    assert s.funding.assets() == 100_100 * 10**6
    shares = s.vault.balanceOf(s.funding.address)
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 60 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 60 * 10**6
    assert s.usdc.balanceOf(s.funding.address) == 40 * 10**6
    assert s.vault.balanceOf(s.funding.address) == shares
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start + WEEK, 100 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 160 * 10**6
    assert s.usdc.balanceOf(s.funding.address) == 0
    assert s.vault.balanceOf(s.funding.address) == shares - 60 * 10**6
    assert s.funding.spent() == 160 * 10**6


def test_failed_partial_vault_withdrawal_rolls_back_liquid_payment(system):
    s = system
    s.usdc.mint(s.funding.address, 25 * 10**6)
    s.vault.configure(UNIT, True)
    with boa.env.prank(s.manager), boa.reverts("withdrawal"):
        s.funding.purchase(s.start, 100 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 0
    assert s.usdc.balanceOf(s.funding.address) == 25 * 10**6
    assert s.funding.paid(s.start) == s.funding.spent() == 0


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
    s.usdc.mint(s.escrow.address, 500 * 10**6)
    s.crvusd.mint(s.escrow.address, 7 * UNIT)
    before_minted = s.minter.minted(s.receiver, s.gauge.address)
    before_returned = s.usdc.balanceOf(s.receiver)
    assert not s.escrow.weeks(s.start).ready
    amount, output = sale(s)
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.minter.minted(s.receiver, s.gauge.address) - before_minted == amount
    assert s.gauge.integrate_fraction(s.receiver) == s.minter.minted(
        s.receiver, s.gauge.address
    )
    assert s.crv.balanceOf(s.receiver) == 900 * UNIT
    assert s.crv.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.usdc.balanceOf(s.escrow.address) == 500 * 10**6 + output // 4
    assert s.crvusd.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.usdc.balanceOf(s.receiver) - before_returned == output - output // 4
    assert preview(s) == (0, 0)
    with boa.env.prank(s.keeper), boa.reverts("nothing minted"):
        s.escrow.sell(0, s.start, 1, 1)


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
        s.escrow.sell(0, s.start, 1, 1)


def test_boundary_tail_uses_new_week_share(system):
    s = system
    begin(s, 2)
    sale(s)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start + 3600)
    advance(s.start + WEEK - 180)
    sale(s)
    advance(s.start + WEEK + 60)
    before = s.usdc.balanceOf(s.escrow.address)
    amount, output = sale(s)
    sold = s.escrow.get_logs()[-1]
    assert amount > 0
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.escrow.weeks(s.start + WEEK).share == UNIT // 2
    assert s.usdc.balanceOf(s.escrow.address) - before == output // 2
    assert sold.week == s.start + WEEK and sold.crv == amount


@pytest.mark.parametrize("when", ["before", "unfunded", "after"])
def test_unfunded_claims_return_everything_to_vyper(system, when):
    s = system
    begin(s)
    retained = s.usdc.balanceOf(s.escrow.address)
    if when == "before":
        # begin() already settled a real pre-programme claim.
        assert retained == 0 and s.usdc.balanceOf(s.receiver) > 0
        return
    advance(s.start + WEEK + 100 if when == "unfunded" else s.funding.END() + 100)
    before = s.usdc.balanceOf(s.receiver)
    _, output = sale(s)
    assert s.usdc.balanceOf(s.receiver) - before == output
    assert s.usdc.balanceOf(s.escrow.address) == retained


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


@pytest.mark.parametrize(
    "failure",
    ["mint", "allowance", "slippage", "first hop", "second hop", "return", "forward"],
)
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
        s.usdc.reject(s.receiver)
    elif failure == "first hop":
        s.pool.configure(UNIT // 4, 9000)
    elif failure == "second hop":
        s.stable_pool.configure(9000)
    elif failure == "forward":
        with boa.env.prank(s.manager):
            s.escrow.set_yearn_receiver(s.stranger)
        s.usdc.reject(s.stranger)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    accrued = s.gauge.integrate_fraction(s.receiver)
    balances = (s.crv.balanceOf(s.receiver), s.usdc.balanceOf(s.receiver))
    amount, output = preview(s)
    with boa.env.prank(s.keeper), boa.reverts():
        s.escrow.sell(
            output + 1 if failure == "slippage" else output, s.start, amount, amount
        )
    assert not s.escrow.weeks(s.start).ready
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.gauge.integrate_fraction(s.receiver) == accrued
    assert (s.crv.balanceOf(s.receiver), s.usdc.balanceOf(s.receiver)) == balances
    assert s.crv.balanceOf(s.escrow.address) == s.usdc.balanceOf(s.escrow.address) == 0
    assert s.crvusd.balanceOf(s.escrow.address) == 0


def test_receiver_changes_only_future_sales_and_can_resume_accumulation(system):
    s = system
    begin(s)
    assert s.escrow.yearn_receiver() == s.escrow.address
    _, output = sale(s)
    held = output // 4
    with boa.env.prank(s.stranger), boa.reverts("manager address"):
        s.escrow.set_yearn_receiver(s.stranger)
    with boa.env.prank(s.manager):
        with boa.reverts("manager address"):
            s.escrow.set_yearn_receiver("0x" + "00" * 20)
        s.escrow.set_yearn_receiver(s.stranger)
    assert s.usdc.balanceOf(s.escrow.address) == held
    advance(s.start + 7200)
    _, output = sale(s)
    event = s.escrow.get_logs()[-1]
    assert event.yearn_receiver == s.stranger and event.yearn == output // 4
    assert s.usdc.balanceOf(s.stranger) == event.yearn
    assert s.usdc.balanceOf(s.escrow.address) == held
    with boa.env.prank(s.manager):
        s.escrow.set_yearn_receiver(s.escrow.address)
    advance(s.start + 10800)
    _, output = sale(s)
    assert s.usdc.balanceOf(s.escrow.address) == held + output // 4


@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_governance_claws_back_funding_without_sale_or_vault_dependencies(system, when):
    s = system
    governance = s.funding.GOVERNANCE()
    if when != "before":
        begin(s)
        if when == "after":
            advance(s.funding.END())
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    s.vault.configure(
        UNIT, True
    )  # Redemption can be unavailable; shares remain recoverable.
    shares = s.vault.balanceOf(s.funding.address)
    spent = s.funding.spent()
    s.usdc.mint(s.funding.address, 123)
    for caller in [s.keeper, s.stranger, s.escrow.address]:
        with boa.env.prank(caller), boa.reverts("management"):
            s.funding.recover(s.vault.address, shares)
    with boa.env.prank(governance):
        s.funding.recover(s.vault.address, shares)
        s.funding.recover(s.usdc.address, 123)
    assert s.vault.balanceOf(governance) == shares
    assert (
        s.vault.balanceOf(s.funding.address) == s.usdc.balanceOf(s.funding.address) == 0
    )
    assert s.usdc.balanceOf(governance) == 123
    assert (
        s.funding.spent() == spent and s.funding.remaining() == 100_000 * 10**6 - spent
    )


@pytest.mark.parametrize("share", [0, 1, 3, 4])
def test_split_extremes_and_rounding_conserve_output(system, share):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, share * 10**12, s.start + 52 * WEEK, s.start - 1)
    before = s.usdc.balanceOf(s.receiver)
    _, output = sale(s)
    retained = output * share // 4
    assert s.usdc.balanceOf(s.escrow.address) == retained
    assert s.usdc.balanceOf(s.receiver) - before == output - retained


def test_zero_weight_week_can_settle_pending_tail(system):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, 0, s.start, s.start - 1)
    s.gauge.set_weight(s.start, 0, 0)
    assert s.escrow.checkpoint() == 0
    _, output = sale(s)  # The unclaimed minute before START still exists.
    assert output > 0 and s.usdc.balanceOf(s.escrow.address) == 0


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
    assert s.usdc.balanceOf(s.escrow.address) == output // 4


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
        s.escrow.sell(0, s.start, 1, 1)
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.keeper, False)
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.sell(0, s.start, 1, 1)


def test_delayed_sale_checks_week_and_scales_floor_for_grown_claim(system):
    s = system
    begin(s, 2)
    amount, output = preview(s)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    advance(s.start + 7200)
    with boa.env.prank(s.keeper), boa.reverts("claim too large"):
        s.escrow.sell(output, s.start, amount, amount)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert not s.escrow.weeks(s.start).ready
    s.stable_pool.configure(9000)
    with boa.env.prank(s.keeper), boa.reverts("slippage"):
        s.escrow.sell(output, s.start, amount, amount * 4)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert not s.escrow.weeks(s.start).ready
    s.stable_pool.configure(10000)
    with boa.env.prank(s.keeper):
        s.escrow.sell(output, s.start, amount, amount * 4)
    assert s.escrow.get_logs()[-1].crv > amount
    minted = s.minter.minted(s.receiver, s.gauge.address)
    advance(s.start + WEEK + 60)
    with boa.env.prank(s.keeper), boa.reverts("stale week"):
        s.escrow.sell(output, s.start, amount, amount * 4)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert not s.escrow.weeks(s.start + WEEK).ready


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
