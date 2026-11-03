"""Real source market calls on a pinned fork. No live transaction is submitted."""

import os
import boa
import pytest
from web3 import Web3
from vyper_grant.core import ADDRESSES, ROOT
from vyper_grant.fees import campaign_fee

BLOCK = 26336208
pytestmark = pytest.mark.fork


@pytest.mark.skipif(
    not os.environ.get("RUN_FORK_TESTS"),
    reason="Set RUN_FORK_TESTS=1 with private RPCs",
)
@pytest.mark.parametrize(
    "v,vm", [(1000 * 10**6, 0), (0, 1000 * 10**6), (500 * 10**6, 500 * 10**6)]
)
def test_real_markets(v, vm):
    url = os.environ["ETHEREUM_RPC_URL"]
    with boa.fork(url, block_identifier=BLOCK, allow_dirty=True):
        treasury, manager, arb = [boa.env.generate_address() for _ in range(3)]
        a = ADDRESSES
        e = boa.load(
            str(ROOT / "contracts/VyperFundingEscrow.vy"),
            a["usdc"],
            treasury,
            manager,
            a["gauge"],
            a["votium"],
            a["remote"],
            a["market"],
            arb,
            100_000,
        )
        token = boa.load_abi(str(ROOT / "abi/usdc.json")).at(a["usdc"])
        holder = "0x55FE002aefF02F77364de339a1292923A15844B8"
        with boa.env.prank(holder):
            token.transfer(e.address, v + vm)
        vt = boa.load_abi(str(ROOT / "abi/votium.json")).at(a["votium"])
        current = vt.activeRound()
        fee = 0
        if vm:
            c = Web3(Web3.HTTPProvider(url))
            campaign = (
                1,
                a["gauge"],
                arb,
                a["usdc"],
                2,
                1000,
                vm,
                [],
                "0x" + "0" * 40,
                False,
            )
            fee = campaign_fee(c, a["remote"], e.address, a["market"], campaign, BLOCK)
        boa.env.set_balance(manager, 10**18)
        with boa.env.prank(manager):
            e.allocate(v, vm, 1000 if vm else 0, [], [], value=fee)
        assert token.balanceOf(e.address) == 0
        assert token.balanceOf(manager) == 0
        assert e.allocated() == v + vm and e.used_rounds(current)
        with boa.env.prank(manager), boa.reverts("round used"):
            e.allocate(v, vm, 1000 if vm else 0, [], [], value=fee)


@pytest.mark.skipif(
    not os.environ.get("RUN_FORK_TESTS") or not os.environ.get("ARBITRUM_RPC_URL"),
    reason="Private Arbitrum fork required",
)
def test_real_arbitrum_delivery_and_refund():
    from eth_abi import encode

    with boa.fork(
        os.environ["ARBITRUM_RPC_URL"], block_identifier=520958459, allow_dirty=True
    ):
        a = ADDRESSES
        origin, treasury, probono = [boa.env.generate_address() for _ in range(3)]
        remote = boa.load_abi(str(ROOT / "abi/remote.json")).at(a["remote"])
        market = boa.load_abi(str(ROOT / "abi/market.json")).at(a["market"])
        token = boa.load_abi(str(ROOT / "abi/wrapped_usdc.json")).at(
            "0xc8a096b7D583FC1CC4692563D61d41642cED8441"
        )
        amount = 1000 * 10**6
        pid = market.campaignCount()
        params = (
            1,
            a["gauge"],
            treasury,
            a["usdc"],
            2,
            1000,
            amount,
            [probono],
            "0x" + "0" * 40,
            False,
        )
        encoded = encode(
            [
                "(uint256,address,address,address,uint8,uint256,uint256,address[],address,bool)"
            ],
            [params],
        )
        payload = encode(
            ["(uint8,address,address,bytes)"], [(0, origin, a["market"], encoded)]
        )
        with boa.env.prank(token.owner()):
            token.mint(a["remote"], amount)
        with boa.reverts():
            remote.receiveMessage(1, a["remote"], payload)
        with boa.env.prank(remote.LA_POSTE()):
            remote.receiveMessage(1, a["remote"], payload)
        cp = market.campaignById(pid)
        assert str(cp[2]) == str(treasury) and str(cp[3]) == str(token.address)
        assert cp[4] == 2 and cp[9] - cp[8] == 2 * 604800
        assert [str(x) for x in market.getAddressesByCampaign(pid)] == [str(probono)]
        with boa.env.prank(origin), boa.reverts():
            market.closeCampaign(pid)
        before = token.balanceOf(treasury)
        with boa.env.prank(treasury):
            market.closeCampaign(pid)
        assert token.balanceOf(treasury) == before + amount
