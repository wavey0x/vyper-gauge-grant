import boa
import pytest

U = 10**6
MAX = 2**256 - 1


def allocate(s, v=1000 * U, vm=0, cap=0, fee=0, vl=(), ml=()):
    e, _, _, _, manager, *_ = s
    with boa.env.prank(manager):
        e.allocate(v, vm, cap, list(vl), list(ml), value=fee)


def ordered(*accounts):
    return sorted(accounts, key=lambda x: int(str(x), 16))


def test_constructor_approves_markets_once(system):
    e, t, m, *_ = system
    assert t.allowance(e.address, m.address) == MAX


def test_allocate_once_per_round_and_never_funds_operator(system):
    e, t, m, treasury, manager, *_ = system
    allocate(system)
    assert e.allocated() == 1000 * U and e.used_rounds(135)
    assert t.balanceOf(manager) == 0 and t.balanceOf(m.address) == 1000 * U
    with boa.reverts("round used"):
        allocate(system)
    m.settings(136, 50, False, False)
    allocate(system)
    assert e.allocated() == 2000 * U


def test_roles(system):
    e, t, m, treasury, manager, receiver, arb, stranger = system
    for caller in [manager, stranger]:
        with boa.env.prank(caller), boa.reverts("treasury"):
            e.recallUSDC(U)
    with boa.env.prank(stranger), boa.reverts("management"):
        e.allocate(U, 0, 0, [], [])
    with boa.env.prank(stranger), boa.reverts("management"):
        e.withdrawUnprocessedVotium(135, U)
    with boa.env.prank(treasury):
        e.recallUSDC(100_000 * U)
    assert t.balanceOf(treasury) == 100_000 * U


def test_votium_exclusions_passed_through(system):
    e, t, m, treasury, manager, receiver, arb, stranger = system
    accounts = ordered(receiver, stranger)
    allocate(system, vl=accounts)
    assert [str(m.last_excluded(i)) for i in range(2)] == [str(a) for a in accounts]


@pytest.mark.parametrize("bad", ["duplicate", "unsorted"])
def test_exclusions_must_be_sorted_unique(system, bad):
    _, _, _, _, _, receiver, _, stranger = system
    a, b = ordered(receiver, stranger)
    with boa.reverts("sorted unique nonzero"):
        allocate(system, vl=[a, a] if bad == "duplicate" else [b, a])


def test_votium_exclusions_disabled_rolls_back(system):
    e, t, m, treasury, manager, receiver, *_ = system
    m.settings(135, 0, False, False)
    with boa.reverts("exclusions disabled"):
        allocate(system, vl=[receiver])
    assert e.allocated() == 0 and not e.used_rounds(135)


def test_vm_fixed_recipient_token_duration_and_exclusions(system):
    e, t, m, treasury, manager, receiver, arb, stranger = system
    allocate(system, v=0, vm=2000 * U, cap=1000, fee=10**12, ml=[receiver])
    c = m.last_campaign()
    assert str(c.gauge) == str(m.address) and str(c.manager) == str(arb)
    assert str(c.rewardToken) == str(t.address) and c.numberOfPeriods == 2
    assert c.isWhitelist is False and [str(a) for a in c.addresses] == [str(receiver)]
    assert t.balanceOf(e.address) == 98000 * U


def test_failed_market_rolls_back_spend_and_round(system):
    e, t, m, *_ = system
    m.settings(135, 50, False, True)
    with boa.reverts("market failed"):
        allocate(system)
    assert e.allocated() == 0 and not e.used_rounds(135)
    assert t.balanceOf(e.address) == 100000 * U


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"v": 25001 * U}, "budget"),
        ({"v": 0}, "budget"),
        ({"fee": 1}, "unused vm"),
        ({"ml": ["0x" + "11" * 20]}, "unused vm"),
        ({"v": 0, "vm": U, "cap": 100001, "fee": 1}, "vote cap"),
        ({"v": 0, "vm": U, "cap": 1, "fee": 0}, "bridge fee"),
        ({"v": 0, "vm": U, "cap": 1, "fee": 10**17 + 1}, "bridge fee"),
    ],
)
def test_gates(system, kwargs, error):
    with boa.reverts(error):
        allocate(system, **kwargs)


def test_total_budget_cannot_be_recycled(system):
    e, t, m, *_ = system
    for n in range(4):
        m.settings(135 + n, 50, False, False)
        allocate(system, v=25000 * U)
    t.mint(e.address, 10000 * U)
    m.settings(139, 50, False, False)
    with boa.reverts("budget"):
        allocate(system)


def test_refund_goes_to_escrow_only(system):
    e, t, m, treasury, manager, *_ = system
    allocate(system)
    before = t.balanceOf(e.address)
    with boa.env.prank(manager):
        e.withdrawUnprocessedVotium(135, U)
    assert t.balanceOf(e.address) == before + U and t.balanceOf(manager) == 0


def test_killed_gauge_cannot_receive_new_incentives(system):
    e, t, m, *_ = system
    m.settings(135, 50, True, False)
    with boa.reverts("gauge killed"):
        allocate(system)
    assert e.allocated() == 0
