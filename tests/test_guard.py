"""The sell-only KeeperGuard: its oracle floor, freshness check, bot restriction and rotation."""

import boa
import pytest
from conftest import ROOT, UNIT

BATCH = 15_000 * UNIT
OUTPUT = 3_750_000_000  # the mock route pays 0.25 USDC per CRV, so 15,000 CRV gives 3,750 USDC


def deploy_guard(s, bot=None):
    return boa.load(str(ROOT / "contracts/KeeperGuard.vy"), s.escrow.address, bot or s.keeper)


def enable(s, guard, *disable):
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(guard.address, True)
        for account in disable:
            s.escrow.set_keeper(account, False)


@pytest.fixture
def guarded(system):
    s = system
    s.guard = deploy_guard(s)
    enable(s, s.guard, s.keeper)
    s.crv.mint(s.receiver, BATCH)
    return s


def floor(answer, amount=BATCH):
    return amount * answer * 9750 // (10000 * 10**20)


def test_floor_matches_the_documented_units():
    assert floor(19_133_139) == 2_798_221_578
    assert floor(25_641_025) <= OUTPUT < floor(25_641_026)


def test_sale_at_the_floor_succeeds_and_one_unit_above_reverts(guarded):
    s = guarded
    s.feed.set(25_641_026, 0)
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(0, BATCH)
    s.feed.set(25_641_025, 0)
    before = s.usdc.balanceOf(s.receiver)
    with boa.env.prank(s.keeper):
        s.guard.work(0, BATCH)
    assert s.usdc.balanceOf(s.receiver) - before == OUTPUT  # Yearn's share is zero this week


def test_output_exactly_at_the_floor_passes_and_one_unit_short_reverts(guarded):
    s = guarded
    s.feed.set(25_000_000, 0)
    assert floor(25_000_000) == 3_656_250_000
    s.pool.configure(10**18 // 4, 9_749)  # pays 3,655,875,000, below the floor
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(0, BATCH)
    s.pool.configure(10**18 // 4, 9_750)  # pays exactly the floor
    before = s.usdc.balanceOf(s.receiver)
    with boa.env.prank(s.keeper):
        s.guard.work(0, BATCH)
    assert s.usdc.balanceOf(s.receiver) - before == 3_656_250_000


def test_the_bots_higher_minimum_still_applies(guarded):
    s = guarded
    s.feed.set(20_000_000, 0)
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(OUTPUT + 1, BATCH)
    with boa.env.prank(s.keeper):
        s.guard.work(OUTPUT, BATCH)


def test_only_the_bot_can_sell_and_only_through_the_guard(guarded):
    s = guarded
    with boa.env.prank(s.stranger), boa.reverts("bot"):
        s.guard.work(0, BATCH)
    with boa.env.prank(s.keeper), boa.reverts("keeper or paused"):
        s.escrow.work(0, BATCH, True)


def test_stale_and_future_prices_revert(guarded):
    s = guarded
    now = boa.env.evm.patch.timestamp
    s.feed.set(25_000_000, now - 90_001)
    with boa.env.prank(s.keeper), boa.reverts("stale"):
        s.guard.work(0, BATCH)
    s.feed.set(25_000_000, now + 1)
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(0, BATCH)
    s.feed.set(25_000_000, now - 90_000)
    with boa.env.prank(s.keeper):
        s.guard.work(0, BATCH)


@pytest.mark.parametrize("answer, amount", [(-1, BATCH), (0, BATCH), (25_000_000, 0), (25_000_000, 1)])
def test_bad_prices_and_amounts_revert(guarded, answer, amount):
    s = guarded
    s.feed.set(answer, 0)
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(0, amount)


def test_constructor_rejects_zero_addresses(system):
    with boa.reverts("address"):
        boa.load(str(ROOT / "contracts/KeeperGuard.vy"), system.escrow.address, "0x" + "00" * 20)


def test_rotation_swaps_guards_in_one_manager_batch(guarded):
    s = guarded
    new_bot = boa.env.generate_address()
    replacement = deploy_guard(s, new_bot)
    assert replacement.BOT() == new_bot and replacement.ESCROW() == s.escrow.address
    enable(s, replacement, s.guard.address)
    with boa.env.prank(s.keeper), boa.reverts():
        s.guard.work(0, BATCH)
    with boa.env.prank(new_bot):
        replacement.work(0, BATCH)
