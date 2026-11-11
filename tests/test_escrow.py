import boa
import pytest
from conftest import ROOT, WEEK, UNIT, advance


def begin(s):
    # Clear historical rewards just before the vote week starts.
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
        s.escrow.work(
            output * 99 // 100,
            boa.env.evm.patch.timestamp // WEEK * WEEK,
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
    assert s.escrow.quote(10 * UNIT) == s.pool.get_dy(2, 0, 10 * UNIT) == 25 * UNIT // 10
    assert s.gauge.last_checkpoint() == last
    amount = s.escrow.claimable()
    assert amount > 0 and s.gauge.last_checkpoint() == boa.env.evm.patch.timestamp
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.crv.balanceOf(s.receiver) == balance
    assert not s.escrow.weeks(s.start).ready
    assert s.escrow.claimable() == amount


def test_draw_can_use_yield_and_topups_without_a_lifetime_cap(system):
    s = system
    s.usdc.mint(s.vault.address, 10_000 * 10**6)
    s.vault.configure(11 * UNIT // 10, False)
    assert s.funding.assets() == 110_000 * 10**6
    with boa.env.prank(s.manager):
        s.funding.draw(110_000 * 10**6)
    assert s.vault.balanceOf(s.funding.address) == s.funding.assets() == 0
    s.usdc.mint(s.manager, 22_000 * 10**6)
    with boa.env.prank(s.manager):
        s.usdc.approve(s.vault.address, 22_000 * 10**6)
        s.vault.deposit(22_000 * 10**6, s.funding.address)
        s.funding.draw(22_000 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 132_000 * 10**6
    assert s.funding.assets() == 0


def test_failed_withdrawal_does_not_record_payment(system):
    s = system
    shares = s.vault.balanceOf(s.funding.address)
    s.vault.configure(UNIT, True)
    with boa.env.prank(s.manager), boa.reverts("withdrawal"):
        s.funding.draw(100 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 0
    assert s.vault.balanceOf(s.funding.address) == shares


def test_draw_withdraws_exact_usdc_and_leaves_loose_tokens_untouched(system):
    s = system
    s.usdc.mint(s.vault.address, 10_000 * 10**6)
    s.vault.configure(11 * UNIT // 10, False)
    s.usdc.mint(s.funding.address, 100 * 10**6)
    assert s.funding.assets() == 110_000 * 10**6
    shares = s.vault.balanceOf(s.funding.address)
    amount = 123_456_789
    burned = (amount * UNIT + s.vault.price() - 1) // s.vault.price()
    with boa.env.prank(s.manager):
        s.funding.draw(amount)
    assert s.usdc.balanceOf(s.payee) == amount
    assert s.usdc.balanceOf(s.funding.address) == 100 * 10**6
    assert s.vault.balanceOf(s.funding.address) == shares - burned


def test_insufficient_shares_revert_even_with_loose_usdc(system):
    s = system
    s.usdc.mint(s.funding.address, 1000 * 10**6)
    shares = s.vault.balanceOf(s.funding.address)
    with boa.env.prank(s.manager), boa.reverts():
        s.funding.draw(100_001 * 10**6)
    assert s.usdc.balanceOf(s.payee) == 0
    assert s.usdc.balanceOf(s.funding.address) == 1000 * 10**6
    assert s.vault.balanceOf(s.funding.address) == shares


def test_draw_permissions_and_no_programme_dates(system):
    s = system
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.funding.draw(1)
    with boa.env.prank(s.manager):
        with boa.reverts("amount"):
            s.funding.draw(0)
        s.funding.draw(1)
        s.funding.draw(2)
        assert s.usdc.balanceOf(s.payee) == 3
        advance(s.start)
        s.funding.draw(4)
        assert s.funding.get_logs()[-1].usdc == 4
        advance(s.start + 8 * WEEK)
        s.funding.draw(8)
    assert s.usdc.balanceOf(s.payee) == 15


def test_funding_recipient_defaults_to_fee_burner_and_can_change(system):
    s = system
    assert s.funding.recipient() == "0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8"
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.funding.set_recipient(s.stranger)
    with boa.env.prank(s.manager):
        for invalid in ["0x" + "00" * 20, s.funding.address]:
            with boa.reverts("recipient"):
                s.funding.set_recipient(invalid)
        s.funding.draw(10 * 10**6)
        s.funding.set_recipient(s.stranger)
        s.funding.draw(20 * 10**6)
        assert s.funding.get_logs()[-1].recipient == s.stranger
    assert s.usdc.balanceOf(s.payee) == 10 * 10**6
    assert s.usdc.balanceOf(s.stranger) == 20 * 10**6


def test_deployment_captures_initial_week_without_dates(system):
    s = system
    assert s.escrow.DEPLOYED_WEEK() == s.start - WEEK
    assert s.escrow.weeks(s.start - WEEK).ready
    assert s.escrow.weeks(s.start - WEEK).share == 0
    assert s.escrow.canWork()
    # If a vote already overwrote this week's history, deployment remains possible
    # but settlement requires an explicit manager repair rather than guessing.
    second = boa.load(str(ROOT / "contracts/ProceedsEscrow.vy"))
    assert not second.weeks(s.start - WEEK).ready
    with boa.reverts("missing vote history"):
        second.checkpoint()
    with boa.env.prank(s.manager):
        second.set_missing_weight(s.start - WEEK, 0)
    assert second.checkpoint() == 0


def test_proceeds_continues_after_funding_is_recovered(system):
    s = system
    with boa.env.prank(s.manager):
        s.funding.draw(2000 * 10**6)
    begin(s)
    _, output = sale(s)
    assert s.crvusd.balanceOf(s.escrow.address) == output // 4
    with boa.env.prank(s.funding.GOVERNANCE()):
        s.funding.recover(s.vault.address, s.vault.balanceOf(s.funding.address))
    s.vault.configure(UNIT, True)
    assert s.funding.assets() == 0
    # The second week still earns its own share, without another draw.
    week = s.start + WEEK
    s.gauge.set_weight(week, 2 * (s.start + 52 * WEEK - week) * 10**12, 10**16)
    advance(week + 3600)
    before = s.crvusd.balanceOf(s.escrow.address)
    _, output = sale(s)
    assert s.escrow.weeks(week).share == UNIT // 2
    assert s.crvusd.balanceOf(s.escrow.address) - before == output // 2


def test_sale_includes_receiver_crv_and_preserves_escrow_balances(system):
    s = system
    begin(s)
    s.crv.mint(s.receiver, 900 * UNIT)
    s.crv.mint(s.escrow.address, 7 * UNIT)
    s.usdc.mint(s.escrow.address, 500 * 10**6)
    s.crvusd.mint(s.escrow.address, 7 * UNIT)
    before_minted = s.minter.minted(s.receiver, s.gauge.address)
    before_returned = s.crvusd.balanceOf(s.receiver)
    assert not s.escrow.weeks(s.start).ready
    amount, output = sale(s)
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.minter.minted(s.receiver, s.gauge.address) - before_minted == amount - 900 * UNIT
    assert s.gauge.integrate_fraction(s.receiver) == s.minter.minted(
        s.receiver, s.gauge.address
    )
    assert s.crv.balanceOf(s.receiver) == 0
    assert s.crv.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.usdc.balanceOf(s.escrow.address) == 500 * 10**6
    assert s.crvusd.balanceOf(s.escrow.address) == 7 * UNIT + output // 4
    assert s.crvusd.balanceOf(s.receiver) - before_returned == output - output // 4
    assert preview(s) == (0, 0)
    with boa.env.prank(s.keeper), boa.reverts("cooldown"):
        s.escrow.work(0, s.start, 1)


def test_public_checkpoint_captures_boundary_without_claiming_while_paused(system):
    s = system
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    advance(s.start)
    with boa.env.prank(s.stranger):
        assert s.escrow.checkpoint() == UNIT // 4
    assert s.minter.minted(s.receiver, s.gauge.address) == 0
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start)
    advance(s.start + 3600)
    assert s.escrow.checkpoint() == UNIT // 4
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.work(0, s.start, 1)


def test_boundary_tail_uses_new_week_share(system):
    s = system
    begin(s)
    sale(s)
    s.gauge.set_vote(s.voter, 2 * 10**12, s.start + 52 * WEEK, s.start + 3600)
    advance(s.start + WEEK - 180)
    sale(s)
    advance(s.start + WEEK + 60)
    assert not s.escrow.canWork()
    with boa.env.prank(s.stranger):
        assert s.escrow.checkpoint() == UNIT // 2
    advance(s.start + WEEK + 420)
    before = s.crvusd.balanceOf(s.escrow.address)
    amount, output = sale(s)
    sold = s.escrow.get_logs()[-1]
    assert amount > 0
    assert s.escrow.weeks(s.start).share == UNIT // 4
    assert s.escrow.weeks(s.start + WEEK).share == UNIT // 2
    assert s.crvusd.balanceOf(s.escrow.address) - before == output // 2
    assert sold.week == s.start + WEEK and sold.crv == amount


def test_deployment_week_with_no_effective_yearn_vote_returns_all_to_vyper(system):
    s = system
    week = s.start - WEEK
    assert s.escrow.weeks(week).ready and s.escrow.weeks(week).share == 0
    # A new vote cast this week only affects next week. Deployment froze the old zero share.
    advance(s.start - 60)
    before = s.crvusd.balanceOf(s.receiver)
    _, output = sale(s)
    sold = s.escrow.get_logs()[-1]
    assert sold.week == week and sold.yearn == 0
    assert s.crvusd.balanceOf(s.receiver) - before == output > 0
    assert s.crvusd.balanceOf(s.escrow.address) == 0
    advance(s.start + 600)
    _, output = sale(s)
    assert s.escrow.get_logs()[-1].week == s.start
    assert s.crvusd.balanceOf(s.escrow.address) == output // 4


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
    ["mint", "allowance", "slippage", "execution", "return", "forward"],
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
        s.crvusd.reject(s.receiver)
    elif failure == "execution":
        s.pool.configure(UNIT // 4, 9000)
    elif failure == "forward":
        with boa.env.prank(s.manager):
            s.escrow.set_yearn_receiver(s.stranger)
        s.crvusd.reject(s.stranger)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    accrued = s.gauge.integrate_fraction(s.receiver)
    balances = (s.crv.balanceOf(s.receiver), s.crvusd.balanceOf(s.receiver))
    last_sale = s.escrow.last_sale()
    amount, output = preview(s)
    with boa.env.prank(s.keeper), boa.reverts():
        s.escrow.work(
            output + 1 if failure == "slippage" else output, s.start, amount
        )
    assert not s.escrow.weeks(s.start).ready
    assert s.escrow.last_sale() == last_sale and s.escrow.canWork()
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.gauge.integrate_fraction(s.receiver) == accrued
    assert (s.crv.balanceOf(s.receiver), s.crvusd.balanceOf(s.receiver)) == balances
    assert s.crv.balanceOf(s.escrow.address) == s.crvusd.balanceOf(s.escrow.address) == 0


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
    assert s.crvusd.balanceOf(s.escrow.address) == held
    advance(s.start + 7200)
    _, output = sale(s)
    event = s.escrow.get_logs()[-1]
    assert event.yearn_receiver == s.stranger and event.yearn == output // 4
    assert s.crvusd.balanceOf(s.stranger) == event.yearn
    assert s.crvusd.balanceOf(s.escrow.address) == held
    with boa.env.prank(s.manager):
        s.escrow.set_yearn_receiver(s.escrow.address)
    advance(s.start + 10800)
    _, output = sale(s)
    assert s.crvusd.balanceOf(s.escrow.address) == held + output // 4


@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_governance_claws_back_funding_without_sale_or_vault_dependencies(system, when):
    s = system
    governance = s.funding.GOVERNANCE()
    if when != "before":
        begin(s)
        if when == "after":
            advance(s.start + 8 * WEEK)
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    s.vault.configure(
        UNIT, True
    )  # Redemption can be unavailable; shares remain recoverable.
    shares = s.vault.balanceOf(s.funding.address)
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


@pytest.mark.parametrize("share", [0, 1, 3, 4])
def test_split_extremes_and_rounding_conserve_output(system, share):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, share * 10**12, s.start + 52 * WEEK, s.start - 1)
    before = s.crvusd.balanceOf(s.receiver)
    _, output = sale(s)
    retained = output * share // 4
    assert s.crvusd.balanceOf(s.escrow.address) == retained
    assert s.crvusd.balanceOf(s.receiver) - before == output - retained


def test_zero_weight_week_can_settle_pending_tail(system):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, 0, s.start, s.start - 1)
    s.gauge.set_weight(s.start, 0, 0)
    assert s.escrow.checkpoint() == 0
    _, output = sale(s)  # The unclaimed minute before the boundary still exists.
    assert output > 0 and s.crvusd.balanceOf(s.escrow.address) == 0


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
    assert s.crvusd.balanceOf(s.escrow.address) == output // 4


def test_already_minted_crv_remains_available_to_sell(system):
    s = system
    begin(s)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(s.stranger)
    with boa.env.prank(s.stranger):
        s.minter.mint_for(s.gauge.address, s.receiver)
    balance = s.crv.balanceOf(s.receiver)
    assert balance > 0 and preview(s)[0] == balance
    minted = s.minter.minted(s.receiver, s.gauge.address)
    amount, _ = sale(s)
    assert amount == balance and s.crv.balanceOf(s.receiver) == 0
    assert s.minter.minted(s.receiver, s.gauge.address) == minted


def test_only_authorized_keepers_sell(system):
    s = system
    begin(s)
    with boa.env.prank(s.stranger), boa.reverts("keeper or paused"):
        s.escrow.work(0, s.start, 1)
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.keeper, False)
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.work(0, s.start, 1)


def test_sale_cooldown_and_manager_floor(system):
    s = system
    begin(s)
    assert s.escrow.cooldown() == 600 and s.escrow.canWork()
    with boa.env.prank(s.stranger), boa.reverts("manager"):
        s.escrow.set_cooldown(3600)
    with boa.env.prank(s.manager), boa.reverts("cooldown"):
        s.escrow.set_cooldown(599)
    sale(s)
    sold_at = s.escrow.last_sale()
    assert not s.escrow.canWork()
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.stranger, True)
    advance(sold_at + 599)
    amount, output = preview(s)
    with boa.env.prank(s.stranger), boa.reverts("cooldown"):
        s.escrow.work(output, s.start, amount)
    assert s.escrow.last_sale() == sold_at
    advance(sold_at + 600)
    assert s.escrow.canWork()
    sale(s)
    with boa.env.prank(s.manager):
        s.escrow.set_cooldown(3600)
    assert not s.escrow.canWork()
    advance(s.escrow.last_sale() + 3600)
    assert s.escrow.canWork()
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    assert not s.escrow.canWork()
    assert s.escrow.checkpoint() == s.escrow.weeks(s.start).share


def test_delayed_sale_caps_pull_and_passes_minimum_unchanged(system):
    s = system
    begin(s)
    amount, output = preview(s)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    advance(s.start + 7200)
    available, _ = preview(s)
    assert available > amount
    s.pool.configure(UNIT // 4, 9000)
    with boa.env.prank(s.keeper), boa.reverts("slippage"):
        s.escrow.work(output, s.start, amount)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert not s.escrow.weeks(s.start).ready
    s.pool.configure(UNIT // 4, 10000)
    with boa.env.prank(s.keeper):
        s.escrow.work(output, s.start, amount)
    event = s.escrow.get_logs()[-1]
    assert event.crv == amount and event.crvusd == output
    assert s.crv.balanceOf(s.receiver) == available - amount
    minted = s.minter.minted(s.receiver, s.gauge.address)
    advance(s.start + WEEK + 60)
    with boa.env.prank(s.keeper), boa.reverts("stale week"):
        s.escrow.work(output, s.start, amount)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert not s.escrow.weeks(s.start + WEEK).ready


def test_capped_batches_can_drain_balance_without_new_mint(system):
    s = system
    begin(s)
    # Exhaust the gauge with a small final mint, then drain it over several calls.
    s.gauge.set_params(
        s.gauge.integrate_fraction(s.receiver) + 25 * UNIT,
        s.gauge.inflation_rate(), s.gauge.future_epoch_time(), False,
    )
    for amount, remainder in [(10 * UNIT, 15 * UNIT), (10 * UNIT, 5 * UNIT), (5 * UNIT, 0)]:
        with boa.env.prank(s.keeper):
            s.escrow.work(s.escrow.quote(amount), s.start, 10 * UNIT)
        assert s.escrow.get_logs()[-1].crv == amount
        assert s.crv.balanceOf(s.receiver) == remainder
        assert preview(s)[0] == remainder
        advance(s.escrow.last_sale() + 600)
    assert s.gauge.is_killed()
    with boa.env.prank(s.keeper), boa.reverts("nothing to sell"):
        s.escrow.work(0, s.start, 10 * UNIT)


def test_zero_cap_rolls_back_claim_and_snapshot(system):
    s = system
    begin(s)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    last_sale = s.escrow.last_sale()
    with boa.env.prank(s.keeper), boa.reverts("nothing to sell"):
        s.escrow.work(0, s.start, 0)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.escrow.last_sale() == last_sale
    assert not s.escrow.weeks(s.start).ready


@pytest.mark.parametrize("role", ["manager", "governance"])
def test_management_withdraws_any_token_to_recipient_while_paused(system, role):
    s = system
    caller = s.manager if role == "manager" else s.escrow.GOVERNANCE()
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
    with boa.env.prank(s.keeper), boa.reverts("management"):
        s.escrow.withdraw(token.address, 123, s.stranger)
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    with boa.env.prank(caller):
        s.escrow.withdraw(token.address, 123, s.stranger)
    event = s.escrow.get_logs()[-1]
    assert event.token == token.address and event.amount == 123
    assert event.recipient == s.stranger
    assert token.balanceOf(s.stranger) == 123
    assert token.balanceOf(s.manager) == token.balanceOf(s.escrow.address) == 0


def test_proceeds_withdrawal_validates_recipient_and_preserves_balance_on_failure(system):
    s = system
    begin(s)
    sale(s)
    held = s.crvusd.balanceOf(s.escrow.address)
    s.crvusd.reject(s.stranger)
    with boa.env.prank(s.manager):
        for invalid in ["0x" + "00" * 20, s.escrow.address]:
            with boa.reverts("recipient"):
                s.escrow.withdraw(s.crvusd.address, held, invalid)
        with boa.reverts("recipient rejected"):
            s.escrow.withdraw(s.crvusd.address, held, s.stranger)
        assert s.crvusd.balanceOf(s.escrow.address) == held
        s.escrow.withdraw(s.crvusd.address, held // 2, s.payee)
    assert s.crvusd.balanceOf(s.payee) == held // 2
    assert s.crvusd.balanceOf(s.escrow.address) == held - held // 2
    assert s.escrow.yearn_receiver() == s.escrow.address
