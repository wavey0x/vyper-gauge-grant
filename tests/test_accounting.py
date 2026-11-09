from random import Random
import boa
import pytest
from conftest import WEEK, UNIT, advance, deploy_proceeds
from test_escrow import begin, sale
from reference import partition, weight


def test_conservative_bound_under_arbitrary_checkpoint_partitions():
    rng = Random(356)
    for _ in range(3000):
        cap = rng.randrange(10**10)
        remaining, upper, actual, assigned = cap, 0, 0, 0
        for _ in range(rng.randint(1, 10)):
            numerator, seconds = rng.randrange(10**20), rng.randrange(1, 10000)
            u, l = numerator * seconds // UNIT, numerator // UNIT * seconds
            raw, left = 0, seconds
            while left:
                dt = rng.randint(1, left)
                raw += numerator * dt // UNIT
                left -= dt
            segment = min(raw, remaining)
            lower = min(l, max(0, cap - upper))
            assert lower <= segment <= u
            assigned, actual = assigned + lower, actual + segment
            upper, remaining = upper + u, remaining - segment
        assert assigned <= actual <= cap


@pytest.mark.parametrize("cut_offset", [-1, 0, 1, 3600])
def test_annual_cut_and_boundary_partition_match_reference(system, cut_offset):
    s = system
    cut = s.start + WEEK + cut_offset
    s.gauge.set_params(s.gauge.max_emissions(), s.gauge.inflation_rate(), cut, False)
    deploy_proceeds(s)
    begin(s, 3)
    anchor = s.escrow.cursor()
    accrued = s.escrow.accrued()
    rate, epoch = s.gauge.inflation_rate(), s.gauge.future_epoch_time()
    previous = {
        w: s.escrow.weeks(w).remaining
        for w in [s.start, s.start + WEEK, s.start + 2 * WEEK]
    }
    for t in [
        s.start + WEEK - 1,
        s.start + WEEK,
        s.start + WEEK + 1,
        s.start + WEEK + 4000,
        s.start + 2 * WEEK + 1,
    ]:
        advance(t)
        s.gauge.user_checkpoint(s.receiver)
    stop = boa.env.evm.patch.timestamp
    weights = {w: s.gauge.gauge_relative_weight(s.gauge.address, w) for w in previous}
    rows, low, high, _, _ = partition(
        anchor, stop, rate, epoch, s.escrow.CAP() - accrued, weights
    )
    s.escrow.sync()
    assert low <= s.escrow.accrued() - accrued <= high
    for w in previous:
        assert s.escrow.weeks(w).remaining - previous[w] == sum(
            n for week, n in rows if week == w
        )


@pytest.mark.parametrize("cap_fraction", [1, 2, 3])
def test_cap_near_boundary_never_assigns_future_week_earnings(system, cap_fraction):
    s = system
    # Activate at the boundary of the preceding week so its cap consumption is explicit.
    per_second = (
        s.gauge.inflation_rate()
        * s.gauge.gauge_relative_weight(s.gauge.address, s.start)
        // UNIT
    )
    cap = per_second * ((s.start - boa.env.evm.patch.timestamp) + WEEK) + cap_fraction
    s.gauge.set_params(
        cap, s.gauge.inflation_rate(), s.gauge.future_epoch_time(), False
    )
    deploy_proceeds(s)
    with boa.env.prank(s.manager):
        s.funding.purchase(s.start, 1)
        s.funding.purchase(s.start + WEEK, 1)
    advance(s.start)
    s.gauge.user_checkpoint(s.receiver)
    before = s.gauge.integrate_fraction(s.receiver)
    advance(s.start + WEEK)
    s.gauge.user_checkpoint(s.receiver)
    first = s.gauge.integrate_fraction(s.receiver) - before
    before = s.gauge.integrate_fraction(s.receiver)
    advance(s.start + WEEK + 3600)
    s.gauge.user_checkpoint(s.receiver)
    second = s.gauge.integrate_fraction(s.receiver) - before
    s.escrow.sync()
    assert s.escrow.weeks(s.start).remaining <= first
    assert s.escrow.weeks(s.start + WEEK).remaining <= second
    assert s.escrow.available() <= first + second


def test_third_party_claims_and_partial_sales_do_not_duplicate_earnings(system):
    s = system
    begin(s)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(s.stranger)
    with boa.env.prank(s.stranger):
        s.minter.mint_for(s.gauge.address, s.receiver)
    advance(s.start + 7200)
    s.escrow.sync()
    sale(s, 10 * UNIT)
    sale(s)
    assert s.escrow.available() == 0
    assert s.escrow.sync() == 0


def test_return_transfer_failure_rolls_back_claim_pull_and_swap(system):
    s = system
    begin(s)
    minted = s.minter.minted(s.receiver, s.gauge.address)
    balance = s.crv.balanceOf(s.receiver)
    s.usd.reject(s.receiver)
    with boa.reverts("recipient rejected"):
        sale(s)
    assert s.minter.minted(s.receiver, s.gauge.address) == minted
    assert s.crv.balanceOf(s.receiver) == balance
    assert s.usd.balanceOf(s.escrow.address) == 0


def test_expired_vote_and_zero_total_are_distinct(system):
    s = system
    s.gauge.set_vote(s.voter, 10**12, s.start, s.start - 1)
    begin(s)
    _, output = sale(s)
    assert s.usd.balanceOf(s.escrow.address) == 0
    assert s.usd.balanceOf(s.receiver) == output
    assert weight(10**12, s.start, s.start) == 0


def test_cap_change_stops_new_accrual_but_recovery_remains_available(system):
    s = system
    begin(s)
    s.gauge.set_params(
        s.gauge.max_emissions() + 1,
        s.gauge.inflation_rate(),
        s.gauge.future_epoch_time(),
        False,
    )
    advance(s.start + 7200)
    with boa.reverts("cap changed"):
        s.escrow.sync()
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
        s.funding.recover(s.vault.address, s.vault.balanceOf(s.funding.address))
    assert s.funding.spent() == 1000 * 10**6
    assert s.funding.remaining() == 99000 * 10**6


def test_final_week_tail_is_booked_and_end_is_terminal(system):
    s = system
    last = s.funding.END() - WEEK
    with boa.env.prank(s.manager):
        s.funding.purchase(last, 1)
    advance(s.funding.END() + 100)
    s.escrow.sync()
    assert s.escrow.weeks(last).remaining > 0
    cursor = s.escrow.cursor()
    available = s.escrow.available()
    advance(s.funding.END() + WEEK)
    assert s.escrow.sync() == available
    assert s.escrow.cursor() == cursor


def test_missing_mint_permission_and_revoked_keeper(system):
    s = system
    begin(s)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(s.escrow.address)
    with boa.reverts("mint permission"):
        sale(s)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(s.escrow.address)
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.keeper, False)
    with boa.reverts("keeper or paused"):
        sale(s)


def test_maximum_catchup_fits_transaction_gas(system):
    s = system
    # Reuse the real contract sources with a full-length programme.
    from conftest import ROOT

    funding = boa.load(
        str(ROOT / "contracts/FundingEscrow.vy"),
        s.payee,
        s.start,
        52,
    )
    escrow = boa.load(
        str(ROOT / "contracts/ProceedsEscrow.vy"),
        funding.address,
    )
    s.usdc.mint(s.manager, 100_000 * 10**6)
    with boa.env.prank(s.manager):
        s.usdc.approve(s.vault.address, 100_000 * 10**6)
        s.vault.deposit(100_000 * 10**6, funding.address)
        for i in range(52):
            funding.purchase(s.start + i * WEEK, 10**6)
    with boa.env.prank(s.receiver):
        s.minter.toggle_approve_mint(escrow.address)
    for w in range(s.start - WEEK, s.start + 65 * WEEK, WEEK):
        s.gauge.set_weight(w, 4 * max(0, s.start + 52 * WEEK - w) * 10**12, 10**16 + 17)
    gas = 0
    for _ in range(4):
        advance(escrow.cursor() + 16 * WEEK)
        escrow.sync()
        gas = max(gas, escrow._computation.get_gas_used())
    assert gas < 15_000_000
    assert escrow.weeks(s.start + 51 * WEEK).ready
    assert escrow.available() > 0
    print("maximum catch-up gas:", gas)


def test_excessive_downtime_preserves_accounting_and_recovery(system):
    s = system
    cursor = s.escrow.cursor()
    advance(cursor + 16 * WEEK + 1)
    with boa.reverts("catch-up limit"):
        s.escrow.sync()
    assert s.escrow.cursor() == cursor
    assert s.escrow.available() == 0
    with boa.env.prank(s.manager):
        s.funding.recover(s.vault.address, s.vault.balanceOf(s.funding.address))
    assert s.vault.balanceOf(s.funding.address) == 0
