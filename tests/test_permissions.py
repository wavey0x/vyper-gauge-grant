"""Exercise every privileged entry point against the intended role boundaries."""

import boa
import pytest
from conftest import advance


@pytest.mark.parametrize("role", ["manager", "governance", "keeper", "receiver", "stranger"])
def test_privileged_entry_points_enforce_roles(system, role):
    s = system
    advance(s.start + 3600)
    governance = s.funding.GOVERNANCE()
    assert s.escrow.GOVERNANCE() == governance
    caller = governance if role == "governance" else getattr(s, role)
    s.crvusd.mint(s.escrow.address, 123)
    s.crv.mint(s.escrow.address, 123)
    with boa.env.anchor():
        amount = s.escrow.claimable()
        minimum = s.escrow.quote(amount) * 99 // 100
    manager = {"manager"}
    management = {"manager", "governance"}
    actions = [
        (s.funding.fund_round, (2,), manager, "manager"),
        (s.funding.cancel, (), management, "management"),
        (s.funding.set_recipient, (s.stranger,), manager, "manager"),
        (s.funding.recover, (s.vault.address, 1), management, "management"),
        (s.escrow.set_missing_weight, (s.start, 0), manager, "manager"),
        (s.escrow.set_yearn_receiver, (s.stranger,), manager, "manager address"),
        (s.escrow.set_keeper, (s.stranger, True), manager, "manager address"),
        (s.escrow.set_cooldown, (900,), manager, "manager"),
        (s.escrow.set_paused, (True,), manager, "manager"),
        (s.escrow.withdraw, (s.crvusd.address, 123, s.stranger), management, "management"),
        (s.escrow.withdraw, (s.crv.address, 123, s.stranger), {"receiver"}, "vyper"),
        (s.escrow.work, (minimum, amount, True), {"keeper"}, "keeper or paused"),
    ]
    for fn, args, allowed, error in actions:
        with boa.env.anchor(), boa.env.prank(caller):
            if role in allowed:
                fn(*args)
            else:
                with boa.reverts(error):
                    fn(*args)


def test_public_helpers_do_not_mint_transfer_or_redirect_funds(system):
    s = system
    advance(s.start + 3600)
    with boa.env.prank(s.manager):
        s.escrow.set_paused(True)
    before = [
        token.balanceOf(account)
        for token in [s.crv, s.crvusd, s.usdc, s.vault]
        for account in [s.funding.address, s.escrow.address, s.receiver, s.stranger]
    ]
    with boa.env.prank(s.stranger):
        assert s.escrow.checkpoint() > 0
        assert s.escrow.claimable() > 0
        assert s.escrow.quote(10**18) > 0
        assert not s.escrow.canWork()
        assert s.funding.assets() > 0
    assert before == [
        token.balanceOf(account)
        for token in [s.crv, s.crvusd, s.usdc, s.vault]
        for account in [s.funding.address, s.escrow.address, s.receiver, s.stranger]
    ]
    assert s.minter.minted(s.receiver, s.gauge.address) == 0
    assert s.escrow.yearn_receiver() == s.escrow.address
    assert s.funding.recipient() == s.payee
