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
        intermediate = attach("token", A["crvusd"])
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
        voter = A["voter"]
        keeper, payee = [boa.env.generate_address() for _ in range(2)]
        start = (boa.env.evm.patch.timestamp // WEEK + 1) * WEEK
        funding = boa.load(
            str(ROOT / "contracts/FundingEscrow.vy"),
            payee,
            start,
            4,
        )
        escrow = boa.load(
            str(ROOT / "contracts/ProceedsEscrow.vy"),
            funding.address,
        )
        holder = "0x55FE002aefF02F77364de339a1292923A15844B8"
        with boa.env.prank(holder):
            usdc.approve(A["vault"], 100_000 * 10**6)
            vault.deposit(100_000 * 10**6, funding.address)
        assert funding.assets() >= 100_000 * 10**6 - 1
        with boa.env.prank(A["receiver"]):
            minter.mint(A["gauge"])
            crv.approve(escrow.address, 10**27)
            if not minter.allowed_to_mint_for(escrow.address, A["receiver"]):
                minter.toggle_approve_mint(escrow.address)
        # Reassign an existing Yearn vote on this pinned fork. No substitute voter.
        previous_gauge = "0x05255C5BD33672b9FEA4129C13274D1E6193312d"
        power = ctrl.vote_user_slopes(voter, previous_gauge)[1]
        assert power > 0
        voted_at = boa.env.evm.patch.timestamp
        with boa.env.prank(voter):
            ctrl.vote_for_gauge_weights(previous_gauge, 0)
            ctrl.vote_for_gauge_weights(A["gauge"], power)
        with boa.env.prank(A["manager"]):
            for i in range(4):
                funding.purchase(start + i * WEEK, 1000 * 10**6)
            escrow.set_keeper(keeper, True)
        assert usdc.balanceOf(payee) == 4000 * 10**6
        assert funding.spent() == 4000 * 10**6
        with boa.env.anchor(), boa.env.prank(A["governance"]):
            shares = vault.balanceOf(funding.address)
            before = vault.balanceOf(A["governance"])
            funding.recover(A["vault"], shares)
            assert vault.balanceOf(A["governance"]) == before + shares
            assert vault.balanceOf(funding.address) == 0
            assert funding.spent() == 4000 * 10**6

        def sell():
            with boa.env.anchor():
                amount = escrow.claimable()
                output = escrow.quote(amount)
            before_crv = crv.balanceOf(A["receiver"])
            before_usd = usdc.balanceOf(A["receiver"])
            before_held = usdc.balanceOf(escrow.address)
            destination = escrow.yearn_receiver()
            before_destination = usdc.balanceOf(destination)
            before_intermediate = intermediate.balanceOf(escrow.address)
            with boa.env.prank(keeper):
                escrow.sell(output * 995 // 1000)
            event = escrow.get_logs(include_child_logs=False)[-1]
            assert event.crv == amount and event.usdc == output
            assert crv.balanceOf(A["receiver"]) == before_crv
            assert crv.balanceOf(escrow.address) == 0
            assert gauge.integrate_fraction(A["receiver"]) == minter.minted(
                A["receiver"], A["gauge"]
            )
            assert event.yearn_receiver == destination
            assert usdc.balanceOf(destination) - before_destination == event.yearn
            assert usdc.balanceOf(escrow.address) - before_held == (
                event.yearn if destination == escrow.address else 0
            )
            assert intermediate.balanceOf(escrow.address) == before_intermediate
            assert usdc.balanceOf(A["receiver"]) - before_usd == output - event.yearn
            return event

        # Start clean, leaving all existing CRV untouched and returning pre-start USD.
        boa.env.time_travel(seconds=start - 180 - boa.env.evm.patch.timestamp)
        assert sell().yearn == 0
        boa.env.time_travel(seconds=3780)
        assert not escrow.weeks(start).ready
        first = sell()
        share = escrow.weeks(start).share
        assert 0 < share < 10**18
        assert first.week == start and first.yearn == first.usdc * share // 10**18
        with boa.env.anchor():
            assert escrow.claimable() == 0

        with boa.env.prank(A["manager"]):
            escrow.set_yearn_receiver(payee)
        boa.env.time_travel(seconds=start + WEEK - 180 - boa.env.evm.patch.timestamp)
        assert sell().week == start
        with boa.env.prank(A["manager"]):
            escrow.set_yearn_receiver(escrow.address)
        boa.env.time_travel(seconds=240)
        tail = sell()
        assert tail.week == start + WEEK
        assert tail.yearn == tail.usdc * escrow.weeks(start + WEEK).share // 10**18

        # Checkpoint before replacing Yearn's vote, after the real ten-day delay.
        boa.env.time_travel(
            seconds=max(start + WEEK + 3600, voted_at + 10 * 86400)
            - boa.env.evm.patch.timestamp
        )
        frozen = escrow.checkpoint()
        with boa.env.prank(voter):
            ctrl.vote_for_gauge_weights(A["gauge"], 0)
        assert escrow.checkpoint() == frozen > 0
        sell()
        boa.env.time_travel(
            seconds=start + 2 * WEEK + 3600 - boa.env.evm.patch.timestamp
        )
        assert sell().yearn == 0
        assert escrow.weeks(start + 2 * WEEK).ready

        # The gauge enforces its own cap; there is no local cap/rate accounting.
        with boa.env.anchor():
            with boa.env.prank(ctrl.admin()):
                ctrl.change_gauge_weight(A["gauge"], 10**36)
            for _ in range(12):
                boa.env.time_travel(seconds=WEEK)
                gauge.user_checkpoint(A["receiver"])
                capped = (
                    gauge.integrate_fraction(A["receiver"]) == gauge.max_emissions()
                )
                final = sell()
                if capped:
                    assert gauge.is_killed() and final.crv > 0
                    with boa.env.anchor():
                        assert escrow.claimable() == 0
                    break
            else:
                raise AssertionError("Gauge did not reach its cap")

        boa.env.time_travel(seconds=funding.END() + 60 - boa.env.evm.patch.timestamp)
        assert sell().yearn == 0
