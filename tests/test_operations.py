import os
import pytest
from web3 import Web3
from vyper_grant.core import ADDRESSES, UNIT, address, artifact, call, contract
from vyper_grant.cli import prepare, campaign_action
from vyper_grant.reconcile import reconcile


@pytest.mark.fork
@pytest.mark.skipif(not os.environ.get("OPS_FORK_RPC"), reason="Local Anvil fork only")
def test_prepare_send_and_reconcile_real_votium(tmp_path, monkeypatch):
    url = os.environ["OPS_FORK_RPC"]
    assert url.startswith(("http://127.0.0.1:", "http://localhost:"))
    w = Web3(Web3.HTTPProvider(url))
    snapshot = w.provider.make_request("evm_snapshot", [])["result"]
    try:
        manager, treasury, arb = w.eth.accounts[:3]
        a = ADDRESSES
        compiled = artifact()
        factory = w.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
        tx = factory.constructor(
            a["usdc"],
            treasury,
            manager,
            a["gauge"],
            a["votium"],
            a["remote"],
            a["market"],
            arb,
            100000,
        ).transact({"from": manager})
        receipt = w.eth.wait_for_transaction_receipt(tx)
        assert receipt["status"] == 1
        escrow = receipt["contractAddress"]
        holder = address("0x55FE002aefF02F77364de339a1292923A15844B8")
        w.provider.make_request("anvil_impersonateAccount", [holder])
        w.provider.make_request("anvil_setBalance", [holder, hex(10**18)])
        t = contract(w, "usdc")
        w.eth.wait_for_transaction_receipt(
            t.functions.transfer(escrow, 100000 * UNIT).transact({"from": holder})
        )
        w.provider.make_request("anvil_mine", [hex(65)])
        config = {
            "escrow": escrow,
            "deployment_block": receipt["blockNumber"],
            "management": manager,
            "treasury": treasury,
            "campaign_manager": arb,
            "addresses": ADDRESSES,
            "exclusions": {"votium": [], "vote_market": []},
            "max_reward_per_vote": 100000,
        }
        batch = prepare(w, None, config, 1000 * UNIT, 0, 0, tmp_path / "report.json")
        original_get_block = w.eth.get_block
        final_number = original_get_block("finalized")["number"]

        def changed_block(identifier, *args, **kwargs):
            value = original_get_block(identifier, *args, **kwargs)
            if identifier == final_number:
                return {**value, "hash": b"z" * 32}
            return value

        with monkeypatch.context() as patch:
            patch.setattr(w.eth, "get_block", changed_block)
            with pytest.raises(ValueError, match="reorganized"):
                reconcile(w, None, config)
        call = batch["transactions"][0]
        tx = w.eth.send_transaction(
            {
                "from": manager,
                "to": call["to"],
                "value": int(call["value"]),
                "data": call["data"],
            }
        )
        assert w.eth.wait_for_transaction_receipt(tx)["status"] == 1
        w.provider.make_request("anvil_mine", [hex(65)])
        result = reconcile(w, None, config)
        assert result["allocated_usdc"] == 1000 * UNIT
        assert result["balance_usdc"] == 99000 * UNIT
        assert result["cost_paid_usdc"] == 20 * UNIT
        assert result["committed_usdc"] == 980 * UNIT
        assert len(result["allocations"]) == 1
        assert result["delivery_pending"] == 0
    finally:
        w.provider.make_request("evm_revert", [snapshot])


@pytest.mark.fork
@pytest.mark.skipif(
    not os.environ.get("OPS_FORK_RPC") or not os.environ.get("OPS_ARB_FORK_RPC"),
    reason="Two local Anvil forks required",
)
def test_mixed_delivery_correlation_and_cancelled_campaign_refund(tmp_path):
    from eth_abi import encode
    from web3.logs import DISCARD

    source = os.environ["OPS_FORK_RPC"]
    destination = os.environ["OPS_ARB_FORK_RPC"]
    assert source.startswith("http://127.0.0.1:") and destination.startswith(
        "http://127.0.0.1:"
    )
    w = Web3(Web3.HTTPProvider(source))
    arb = Web3(Web3.HTTPProvider(destination))
    snapshots = [
        x.provider.make_request("evm_snapshot", [])["result"] for x in [w, arb]
    ]
    try:
        manager, treasury = w.eth.accounts[:2]
        a = ADDRESSES
        campaign_manager = address("0xFE11a5009f2121622271e7dd0FD470264e076af6")
        assert arb.eth.get_code(campaign_manager)
        compiled = artifact()
        factory = w.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
        receipt = w.eth.wait_for_transaction_receipt(
            factory.constructor(
                a["usdc"],
                treasury,
                manager,
                a["gauge"],
                a["votium"],
                a["remote"],
                a["market"],
                campaign_manager,
                100000,
            ).transact({"from": manager})
        )
        escrow = receipt["contractAddress"]
        holder = address("0x55FE002aefF02F77364de339a1292923A15844B8")
        w.provider.make_request("anvil_impersonateAccount", [holder])
        w.provider.make_request("anvil_setBalance", [holder, hex(10**18)])
        w.eth.wait_for_transaction_receipt(
            contract(w, "usdc")
            .functions.transfer(escrow, 100000 * UNIT)
            .transact({"from": holder})
        )
        w.provider.make_request("anvil_mine", [hex(65)])
        config = {
            "escrow": escrow,
            "deployment_block": receipt["blockNumber"],
            "arb_scan_from_block": arb.eth.block_number,
            "management": manager,
            "treasury": treasury,
            "campaign_manager": campaign_manager,
            "addresses": ADDRESSES,
            "exclusions": {"votium": [], "vote_market": [a["convex_voter"]]},
            "max_reward_per_vote": 100000,
        }
        batch = prepare(
            w, arb, config, 500 * UNIT, 500 * UNIT, 1000, tmp_path / "mixed.json"
        )
        tx = batch["transactions"][0]
        receipt = w.eth.wait_for_transaction_receipt(
            w.eth.send_transaction(
                {
                    "from": manager,
                    "to": tx["to"],
                    "data": tx["data"],
                    "value": int(tx["value"]),
                }
            )
        )
        assert receipt["status"] == 1
        lp_addr = call(w, a["remote"], "LA_POSTE", outputs=("address",))
        lp = contract(w, "laposte", lp_addr)
        sent = lp.events.MessageSent().process_receipt(receipt, errors=DISCARD)
        assert len(sent) == 1
        m = sent[0]["args"]["message"]
        full = (
            m["destinationChainId"],
            m["to"],
            m["sender"],
            [(x["tokenAddress"], x["amount"]) for x in m["tokens"]],
            [(x["name"], x["symbol"], x["decimals"]) for x in m["tokenMetadata"]],
            m["payload"],
            m["nonce"],
        )
        encoded = encode(
            [
                "(uint256,address,address,(address,uint256)[],(string,string,uint8)[],bytes,uint256)"
            ],
            [full],
        )
        dest_lp = contract(arb, "laposte", lp_addr)
        adapter = dest_lp.functions.adapter().call()
        arb.provider.make_request("anvil_impersonateAccount", [adapter])
        arb.provider.make_request("anvil_setBalance", [adapter, hex(10**18)])
        delivered = arb.eth.wait_for_transaction_receipt(
            dest_lp.functions.receiveMessage(1, encoded).transact(
                {"from": adapter, "gas": 4000000}
            )
        )
        assert delivered["status"] == 1
        received = dest_lp.events.MessageReceived().process_receipt(
            delivered, errors=DISCARD
        )
        assert len(received) == 1 and received[0]["args"]["success"]
        w.provider.make_request("anvil_mine", [hex(65)])
        arb.provider.make_request("anvil_mine", [hex(65)])
        result = reconcile(w, arb, config)
        assert len(result["campaigns"]) == 1 and result["delivery_pending"] == 0
        campaign = result["campaigns"][0]
        assert (
            result["committed_usdc"] == 990 * UNIT
            and result["cost_paid_usdc"] == 10 * UNIT
        )
        arb.provider.make_request("anvil_impersonateAccount", [campaign_manager])
        arb.provider.make_request("anvil_setBalance", [campaign_manager, hex(10**18)])
        recovery = campaign_action(arb, config, campaign["id"], "close")
        assert (
            recovery["chainId"] == "42161"
            and recovery["meta"]["safeAddress"] == campaign_manager
        )
        recovery_tx = recovery["transactions"][0]
        closed = arb.eth.wait_for_transaction_receipt(
            arb.eth.send_transaction(
                {
                    "from": campaign_manager,
                    "to": recovery_tx["to"],
                    "data": recovery_tx["data"],
                    "value": int(recovery_tx["value"]),
                }
            )
        )
        assert closed["status"] == 1
        arb.provider.make_request("anvil_mine", [hex(65)])
        result = reconcile(w, arb, config)
        assert result["refunds_usdc"] == 500 * UNIT
        assert (
            result["cost_paid_usdc"] == 10 * UNIT
            and result["committed_usdc"] == 490 * UNIT
        )
        assert result["campaigns"][0]["paid"] == 0 and result["campaigns"][0]["closed"]
    finally:
        for client, snapshot in zip([w, arb], snapshots):
            client.provider.make_request("evm_revert", [snapshot])


@pytest.mark.fork
@pytest.mark.skipif(
    not os.environ.get("OPS_ARB_FORK_RPC"), reason="Local Arbitrum fork required"
)
def test_votemarket_can_simulate_unclaimed_period_without_transaction():
    from eth_abi import encode
    from vyper_grant.reconcile import vm_period

    url = os.environ["OPS_ARB_FORK_RPC"]
    assert url.startswith("http://127.0.0.1:")
    w = Web3(Web3.HTTPProvider(url))
    snapshot = w.provider.make_request("evm_snapshot", [])["result"]
    try:
        m = contract(w, "market")
        pid = 2108
        week = 1793232000
        original = m.functions.periodByCampaignId(pid, week).call()
        assert original[3]
        # Make the existing period unprocessed on this disposable fork. Locate its
        # nested mapping by matching the original data, rather than guessing a slot.
        matches = []
        for slot in range(32):
            outer = Web3.keccak(encode(["uint256", "uint256"], [pid, slot]))
            base = int.from_bytes(
                Web3.keccak(encode(["uint256", "bytes32"], [week, outer])), "big"
            )
            if (
                int.from_bytes(w.eth.get_storage_at(m.address, base), "big")
                == original[0]
                and int.from_bytes(w.eth.get_storage_at(m.address, base + 1), "big")
                == original[1]
            ):
                matches.append(base)
        assert len(matches) == 1
        w.provider.make_request(
            "anvil_setStorageAt", [m.address, hex(matches[0] + 3), "0x" + "00" * 32]
        )
        assert not m.functions.periodByCampaignId(pid, week).call()[3]
        period = vm_period(w, m, pid, week, "latest")
        assert period["updated"] is True
        assert not m.functions.periodByCampaignId(pid, week).call()[3]
    finally:
        w.provider.make_request("evm_revert", [snapshot])
