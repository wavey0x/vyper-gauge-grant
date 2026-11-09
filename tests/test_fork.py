"""Real vault withdrawals, controller votes, gauge claims and swaps; no live writes."""

import json
import os
import boa
import pytest
from vyper_grant.core import ADDRESSES as A, ROOT, WEEK, abi

BLOCK = 26379075
pytestmark = [
    pytest.mark.fork,
    pytest.mark.skipif(
        not os.environ.get("RUN_FORK_TESTS"),
        reason="Set RUN_FORK_TESTS=1 with ETHEREUM_RPC_URL",
    ),
]


def test_real_vault_gauge_week_boundaries_and_atomic_sales():
    with boa.fork(
        os.environ["ETHEREUM_RPC_URL"], block_identifier=BLOCK, allow_dirty=True
    ):
        attach = lambda name, addr: boa.loads_abi(json.dumps(abi(name))).at(addr)
        vault = attach("vault", A["vault"])
        usdc = attach("token", A["usdc"])
        crv = attach("token", A["crv"])
        usd = attach("token", A["crvusd"])
        gauge = attach("gauge", A["gauge"])
        minter = attach("minter", A["minter"])
        ctrl_abi = abi("controller") + [
            {
                "name": "vote_for_gauge_weights",
                "type": "function",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "gauge", "type": "address"},
                    {"name": "weight", "type": "uint256"},
                ],
                "outputs": [],
            }
        ]
        ctrl_abi += [
            {
                "name": "admin",
                "type": "function",
                "stateMutability": "view",
                "inputs": [],
                "outputs": [{"type": "address"}],
            },
            {
                "name": "change_gauge_weight",
                "type": "function",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "gauge", "type": "address"},
                    {"name": "weight", "type": "uint256"},
                ],
                "outputs": [],
            },
        ]
        ctrl = boa.loads_abi(json.dumps(ctrl_abi)).at(A["controller"])
        voter, keeper, payee = [boa.env.generate_address() for _ in range(3)]
        start = (boa.env.evm.patch.timestamp // WEEK + 1) * WEEK
        funding = boa.load(
            str(ROOT / "contracts/FundingEscrow.vy"),
            A["vault"],
            A["manager"],
            payee,
            start,
            52,
        )
        escrow = boa.load(
            str(ROOT / "contracts/ProceedsEscrow.vy"),
            funding.address,
            A["crv"],
            A["crvusd"],
            A["gauge"],
            A["controller"],
            A["minter"],
            voter,
            A["pool"],
        )
        holder = "0x55FE002aefF02F77364de339a1292923A15844B8"
        with boa.env.prank(holder):
            usdc.approve(A["vault"], 100_000 * 10**6)
            vault.deposit(100_000 * 10**6, funding.address)
        assert funding.assets() >= 100_000 * 10**6 - 1
        with boa.env.prank(A["receiver"]):
            minter.mint(A["gauge"])
            crv.transfer(voter, 1000 * 10**18)
            crv.approve(escrow.address, 10**27)
            if not minter.allowed_to_mint_for(escrow.address, A["receiver"]):
                minter.toggle_approve_mint(escrow.address)
        ve_abi = [
            {
                "name": "create_lock",
                "type": "function",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "value", "type": "uint256"},
                    {"name": "unlock_time", "type": "uint256"},
                ],
                "outputs": [],
            }
        ]
        ve = boa.loads_abi(json.dumps(ve_abi)).at(
            "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"
        )
        voted_at = boa.env.evm.patch.timestamp
        with boa.env.prank(voter):
            crv.approve(ve.address, 1000 * 10**18)
            ve.create_lock(1000 * 10**18, voted_at + 4 * 365 * 86400)
            ctrl.vote_for_gauge_weights(A["gauge"], 10000)
        with boa.env.prank(A["manager"]):
            for i in range(52):
                funding.purchase(start + i * WEEK, 1000 * 10**6)
            escrow.set_keeper(keeper, True)
        assert usdc.balanceOf(payee) == 52000 * 10**6
        assert funding.spent() == 52000 * 10**6
        baseline = escrow.accrued()
        boa.env.time_travel(seconds=start + 3600 - boa.env.evm.patch.timestamp)
        with boa.env.anchor():
            amount = escrow.sync()
            first = escrow.weeks(start)
            assert first.yearn > 0 and first.yearn < first.total
        assert escrow.available() == 0  # The actual sale must perform its own sync.
        quote = escrow.quote(amount)
        before = usd.balanceOf(A["receiver"])
        with boa.env.prank(keeper):
            escrow.sell(amount, quote * 995 // 1000)
        assert escrow.available() == 0
        assert usd.balanceOf(escrow.address) == quote * first.yearn // first.total
        assert (
            usd.balanceOf(A["receiver"]) - before
            == quote - quote * first.yearn // first.total
        )
        assert (escrow.available() + amount) <= escrow.accrued() - baseline
        # The next week is captured before a revote, after the controller's ten-day delay.
        boa.env.time_travel(
            seconds=max(start + WEEK + 3600, voted_at + 10 * 86400)
            - boa.env.evm.patch.timestamp
        )
        escrow.sync()
        frozen = escrow.weeks(start + WEEK)
        with boa.env.prank(voter):
            ctrl.vote_for_gauge_weights(A["gauge"], 0)
        escrow.sync()
        assert escrow.weeks(start + WEEK).yearn == frozen.yearn > 0
        boa.env.time_travel(
            seconds=start + 2 * WEEK + 3600 - boa.env.evm.patch.timestamp
        )
        gauge.user_checkpoint(A["receiver"])
        escrow.sync()
        assert escrow.weeks(start + 2 * WEEK).ready
        assert escrow.weeks(start + 2 * WEEK).yearn == 0
        assert (escrow.available() + amount) <= escrow.accrued() - baseline
        # Reach the actual gauge cap on a fork-only governance branch. Its public
        # is_killed() now returns True, but the final accrued CRV must remain saleable.
        with boa.env.anchor():
            with boa.env.prank(ctrl.admin()):
                ctrl.change_gauge_weight(A["gauge"], 10**36)
            reached = False
            for _ in range(12):
                boa.env.time_travel(seconds=WEEK)
                before_available = escrow.available()
                gauge.user_checkpoint(A["receiver"])
                reached = (
                    gauge.integrate_fraction(A["receiver"]) == gauge.max_emissions()
                )
                escrow.sync()
                if reached:
                    assert gauge.is_killed()
                    assert escrow.available() > before_available
                    quote = escrow.quote(10**18)
                    with boa.env.prank(keeper):
                        escrow.sell(
                            10**18,
                            quote * 995 // 1000,
                        )
                    break
            assert reached
        gas = 0
        for _ in range(4):
            boa.env.time_travel(seconds=16 * WEEK)
            escrow.sync()
            gas = max(gas, escrow._computation.get_gas_used())
        assert escrow.weeks(start + 51 * WEEK).ready
        assert gas < 15_000_000
        print("mainnet maximum 16-week catch-up gas:", gas)
