"""Vyper's retained CRV cannot be sold by later work calls or withdrawn by Yearn."""

import boa
import pytest
from conftest import UNIT, WEEK, advance
from test_escrow import begin, preview, sale


@pytest.mark.parametrize("forward", [False, True])
def test_retaining_then_selling_preserves_previous_vyper_crv(system, forward):
    s = system
    begin(s)
    s.crv.mint(s.escrow.address, 7 * UNIT)
    if forward:
        with boa.env.prank(s.manager):
            s.escrow.set_yearn_receiver(s.payee)
    amount = 17 * UNIT + 3
    assert preview(s)[0] > amount
    swapped = amount // 4
    output = s.escrow.quote(swapped)
    destination = s.payee if forward else s.escrow.address
    before = s.usdc.balanceOf(destination)
    before_vyper_usd = s.usdc.balanceOf(s.receiver)
    with boa.env.prank(s.keeper):
        s.escrow.work(output, amount, False)
    event = s.escrow.get_logs()[-1]
    reserved = 7 * UNIT + amount - swapped
    assert event.crv == swapped and event.vyper_crv == amount - swapped
    assert event.yearn == event.usdc == output
    assert s.usdc.balanceOf(destination) - before == output
    assert s.usdc.balanceOf(s.receiver) == before_vyper_usd
    assert s.crv.balanceOf(s.escrow.address) == reserved
    advance(s.escrow.last_sale() + 600)
    sale(s)  # True sells the new batch, without selling any retained CRV.
    assert s.escrow.get_logs()[-1].vyper_crv == 0
    assert s.crv.balanceOf(s.escrow.address) == reserved
    assert preview(s) == (0, 0)  # Held CRV is never included in claimable().


@pytest.mark.parametrize("share", [0, 1, 4])
def test_retained_split_extremes_and_rounding(system, share):
    s = system
    begin(s)
    s.gauge.set_vote(s.voter, share * 10**12, s.start + 52 * WEEK, s.start - 1)
    amount = 17 * UNIT + 3
    swapped = amount * share // 4
    output = s.escrow.quote(swapped)
    before_pool = s.crv.balanceOf(s.pool.address)
    with boa.env.prank(s.keeper):
        s.escrow.work(output, amount, False)
    event = s.escrow.get_logs()[-1]
    assert event.crv + event.vyper_crv == amount
    assert event.crv == swapped and event.yearn == event.usdc == output
    assert s.crv.balanceOf(s.pool.address) - before_pool == swapped
    assert s.crv.balanceOf(s.escrow.address) == amount - swapped
    assert s.usdc.balanceOf(s.escrow.address) == output
    assert not s.escrow.canWork()  # The cooldown also applies when no swap occurred.


def test_zero_yearn_share_requires_zero_minimum_and_skips_swap(system):
    s = system  # Deployment week's share is zero.
    advance(s.start - 180)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    with boa.env.prank(s.keeper), boa.reverts("min out"):
        s.escrow.work(1, 10 * UNIT, False)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    before_pool = s.crv.balanceOf(s.pool.address)
    with boa.env.prank(s.keeper):
        s.escrow.work(0, 10 * UNIT, False)
    assert s.crv.balanceOf(s.pool.address) == before_pool
    assert s.crv.balanceOf(s.escrow.address) == 10 * UNIT
    assert s.escrow.get_logs()[-1].usdc == 0


def test_retained_crv_cannot_be_sold_after_gauge_exhaustion(system):
    s = system
    begin(s)
    s.gauge.set_params(
        s.gauge.integrate_fraction(s.receiver) + 20 * UNIT,
        s.gauge.inflation_rate(), s.gauge.future_epoch_time(), False,
    )
    with boa.env.prank(s.keeper):
        s.escrow.work(s.escrow.quote(5 * UNIT), 20 * UNIT, False)
    assert s.crv.balanceOf(s.escrow.address) == 15 * UNIT
    advance(s.escrow.last_sale() + 600)
    assert preview(s) == (0, 0)
    with boa.env.prank(s.keeper), boa.reverts("nothing to sell"):
        s.escrow.work(0, 15 * UNIT, True)
    assert s.crv.balanceOf(s.escrow.address) == 15 * UNIT


def test_vyper_withdraws_crv_while_paused_without_access_to_other_tokens(system):
    s = system
    s.crv.mint(s.escrow.address, 10 * UNIT)
    s.usdc.mint(s.escrow.address, 10**6)
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    for account in [s.manager, s.escrow.GOVERNANCE(), s.keeper, s.stranger]:
        with boa.env.prank(account), boa.reverts("vyper"):
            s.escrow.withdraw(s.crv.address, UNIT, s.payee)
    with boa.env.prank(s.receiver):
        with boa.reverts("management"):
            s.escrow.withdraw(s.usdc.address, 10**6, s.payee)
        for recipient in ["0x" + "00" * 20, s.escrow.address]:
            with boa.reverts("recipient"):
                s.escrow.withdraw(s.crv.address, UNIT, recipient)
        s.crv.reject(s.payee)
        with boa.reverts("recipient rejected"):
            s.escrow.withdraw(s.crv.address, UNIT, s.payee)
        s.escrow.withdraw(s.crv.address, 3 * UNIT, s.stranger)
    assert s.crv.balanceOf(s.stranger) == 3 * UNIT
    assert s.crv.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.usdc.balanceOf(s.escrow.address) == 10**6


def test_failed_yearn_forward_rolls_back_new_crv_retention(system):
    s = system
    begin(s)
    s.crv.mint(s.escrow.address, 7 * UNIT)
    with boa.env.prank(s.manager):
        s.escrow.set_yearn_receiver(s.stranger)
    s.usdc.reject(s.stranger)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    last_sale = s.escrow.last_sale()
    with boa.env.prank(s.keeper), boa.reverts("recipient rejected"):
        s.escrow.work(s.escrow.quote(UNIT), 4 * UNIT, False)
    assert s.crv.balanceOf(s.escrow.address) == 7 * UNIT
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.escrow.last_sale() == last_sale
    assert not s.escrow.weeks(s.start).ready
