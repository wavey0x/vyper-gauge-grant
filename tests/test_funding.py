"""Two fixed weekly releases, independently of the proceeds escrow."""

import boa
import pytest
from conftest import WEEK, UNIT, advance


def schedule(s, amount):
    with boa.env.prank(s.manager):
        s.funding.fund_round(amount)


@pytest.mark.parametrize("offset", [0, 1, 3 * 86400, WEEK - 1])
def test_two_payments_follow_week_boundaries_not_scheduling_time(system, offset):
    s = system
    advance(s.start + offset)
    schedule(s, 20_000 * 10**6 + 1)
    first = s.start + WEEK
    assert s.funding.next_week() == first
    assert s.funding.payments(0) == 10_000 * 10**6
    assert s.funding.payments(1) == 10_000 * 10**6
    assert s.usdc.balanceOf(s.payee) == 0
    assert not s.funding.canWork()
    advance(first - 1)
    with boa.reverts("not due"):
        s.funding.work()
    advance(first)
    assert s.funding.canWork()
    with boa.env.prank(s.stranger):
        s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 10_000 * 10**6
    assert s.funding.next_week() == first + WEEK
    assert s.funding.payments(0) == 10_000 * 10**6
    assert s.funding.payments(1) == 0
    with boa.reverts("not due"):
        s.funding.work()
    advance(first + WEEK - 1)
    assert not s.funding.canWork()
    advance(first + WEEK)
    s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 20_000 * 10**6
    assert s.funding.next_week() == s.funding.payments(0) == s.funding.payments(1) == 0
    assert not s.funding.canWork()
    with boa.reverts("not due"):
        s.funding.work()


def test_late_work_does_not_shift_the_second_boundary_or_repeat_payments(system):
    s = system
    schedule(s, 20_000 * 10**6)
    advance(s.start + WEEK + 300)
    s.funding.work()
    assert s.funding.next_week() == s.start + WEEK
    assert s.funding.canWork()  # Both are overdue; each call releases one payment.
    s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 20_000 * 10**6
    assert not s.funding.canWork()
    advance(s.start + 20 * WEEK)
    with boa.reverts("not due"):
        s.funding.work()


def test_schedule_rejects_empty_or_overlapping_rounds(system):
    s = system
    with boa.env.prank(s.manager):
        for amount in [0, 1]:
            with boa.reverts("amount"):
                s.funding.fund_round(amount)
        s.funding.fund_round(2)
        with boa.reverts("pending round"):
            s.funding.fund_round(10**6)
    advance(s.start)
    s.funding.work()
    with boa.env.prank(s.manager), boa.reverts("pending round"):
        s.funding.fund_round(10**6)
    advance(s.start + WEEK)
    s.funding.work()
    schedule(s, 10**6)
    assert s.funding.next_week() == s.start + 2 * WEEK


def test_failed_redemption_preserves_payment_and_can_be_retried(system):
    s = system
    schedule(s, 200 * 10**6)
    advance(s.start)
    shares = s.vault.balanceOf(s.funding.address)
    s.vault.configure(UNIT, True)
    with boa.reverts("withdrawal"):
        s.funding.work()
    assert s.funding.next_week() == s.start and s.funding.payments(0) == 100 * 10**6
    assert s.usdc.balanceOf(s.payee) == 0
    assert s.vault.balanceOf(s.funding.address) == shares
    s.vault.configure(UNIT, False)
    s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 100 * 10**6


def test_payments_use_exact_vault_withdrawals_and_leave_loose_usdc(system):
    s = system
    s.usdc.mint(s.vault.address, 10_000 * 10**6)
    s.vault.configure(11 * UNIT // 10, False)
    s.usdc.mint(s.funding.address, 100 * 10**6)
    shares = s.vault.balanceOf(s.funding.address)
    amount = 123_456_789
    schedule(s, amount * 2)
    assert s.funding.assets() == 110_000 * 10**6
    advance(s.start)
    s.funding.work()
    burned = (amount * UNIT + s.vault.price() - 1) // s.vault.price()
    assert s.usdc.balanceOf(s.payee) == amount
    assert s.usdc.balanceOf(s.funding.address) == 100 * 10**6
    assert s.vault.balanceOf(s.funding.address) == shares - burned


def test_insufficient_shares_keep_payment_pending_until_topup(system):
    s = system
    s.usdc.mint(s.funding.address, 1000 * 10**6)
    schedule(s, 200_002 * 10**6)
    advance(s.start)
    with boa.reverts():
        s.funding.work()
    assert s.funding.payments(0) == 100_001 * 10**6
    assert s.usdc.balanceOf(s.payee) == 0
    s.usdc.mint(s.manager, 2 * 10**6)
    with boa.env.prank(s.manager):
        s.usdc.approve(s.vault.address, 2 * 10**6)
        s.vault.deposit(2 * 10**6, s.funding.address)
    s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 100_001 * 10**6
    assert s.usdc.balanceOf(s.funding.address) == 1000 * 10**6


@pytest.mark.parametrize("after_first", [False, True])
@pytest.mark.parametrize("role", ["manager", "governance"])
def test_cancellation_and_clawback_prevent_payments_after_topups(system, role, after_first):
    s = system
    caller = s.manager if role == "manager" else s.funding.GOVERNANCE()
    schedule(s, 20_000 * 10**6)
    advance(s.start)
    if after_first:
        s.funding.work()
    with boa.env.anchor(), boa.env.prank(caller):
        s.funding.cancel()
        assert not s.funding.canWork()
        assert s.funding.payments(0) == s.funding.payments(1) == 0
    shares = s.vault.balanceOf(s.funding.address)
    with boa.env.prank(caller):
        s.funding.recover(s.vault.address, shares)
        s.vault.transfer(s.funding.address, shares)
    assert not s.funding.canWork()
    assert s.funding.next_week() == 0
    with boa.reverts("not due"):
        s.funding.work()


def test_recipient_changes_apply_to_pending_payments(system):
    s = system
    assert s.funding.recipient() == "0xb911Fcce8D5AFCEc73E072653107260bb23C1eE8"
    schedule(s, 20 * 10**6)
    advance(s.start)
    s.funding.work()
    with boa.env.prank(s.manager):
        for recipient in ["0x" + "00" * 20, s.funding.address]:
            with boa.reverts("recipient"):
                s.funding.set_recipient(recipient)
        s.funding.set_recipient(s.stranger)
    advance(s.start + WEEK)
    s.funding.work()
    assert s.usdc.balanceOf(s.payee) == 10 * 10**6
    assert s.usdc.balanceOf(s.stranger) == 10 * 10**6
    assert s.funding.get_logs()[-1].recipient == s.stranger


def test_recovery_of_unrelated_token_preserves_schedule(system):
    s = system
    schedule(s, 20 * 10**6)
    s.usdc.mint(s.funding.address, 7)
    with boa.env.prank(s.manager):
        s.funding.recover(s.usdc.address, 7)
    assert s.funding.next_week() == s.start
    assert s.funding.payments(0) == s.funding.payments(1) == 10 * 10**6
