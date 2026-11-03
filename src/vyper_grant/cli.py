from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from web3 import Web3
from .core import (
    ROUND_CAP,
    TARGET,
    WEEK,
    ZERO,
    address,
    artifact,
    atomic_json,
    call,
    check_price,
    contract,
    load_config,
    named,
    rpc,
    sorted_addresses,
    usdc,
)
from .chain import verify_escrow
from .fees import campaign_fee
from .reconcile import reconcile, round_start, vm_period

MAX_BRIDGE_FEE = 10**17


def safe_batch(to, data, value=0, chain_id=1, **meta):
    return {
        "version": "1.0",
        "chainId": str(chain_id),
        "meta": {"name": "Vyper gauge funding", **meta},
        "transactions": [
            {
                "to": to,
                "value": str(value),
                "data": data,
                "contractMethod": None,
                "contractInputsValues": None,
            }
        ],
    }


def require_safe(client, account):
    if not client.eth.get_code(account):
        raise ValueError("Management Safe is not deployed on this chain")
    threshold = call(client, account, "getThreshold")
    owners = call(client, account, "getOwners", outputs=("address[]",))
    if threshold < 1 or len(owners) < threshold:
        raise ValueError("Management address is not a configured Safe")


def deployment(client, arb, c, deployer):
    for key in ["treasury", "management", "campaign_manager"]:
        c[key] = address(c.get(key))
    if c["treasury"] != client.ens.address("ychad.eth"):
        raise ValueError("Treasury differs from current ychad.eth resolution")
    require_safe(client, c["management"])
    readiness = []
    if not arb or not arb.eth.get_code(c["campaign_manager"]):
        readiness.append(
            "Management Safe must be deployed on Arbitrum before enabling VoteMarket"
        )
    else:
        require_safe(arb, c["campaign_manager"])
    deployer = address(deployer)
    a = c["addresses"]
    args = [
        a["usdc"],
        c["treasury"],
        c["management"],
        a["gauge"],
        a["votium"],
        a["remote"],
        a["market"],
        c["campaign_manager"],
        int(c["max_reward_per_vote"]),
    ]
    compiled = artifact()
    factory = client.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx = factory.constructor(*args).build_transaction(
        {"from": deployer, "nonce": client.eth.get_transaction_count(deployer)}
    )
    return {
        "chainId": 1,
        "from": deployer,
        "data": tx["data"],
        "value": "0",
        "gas": tx["gas"],
        "readiness_warnings": readiness,
        "constructor_arguments": args,
        "abi": compiled["abi"],
    }


def prepare(client, arb, c, v, vm, cap, report_path):
    """Reconcile, validate and simulate one allocation; return a Safe batch."""
    e = verify_escrow(client, c)
    a = c["addresses"]
    votium = contract(client, "votium", a["votium"])
    head = client.eth.get_block("latest")
    block = head["number"]
    check_price(
        contract(client, "feed", a["feed"])
        .functions.latestRoundData()
        .call(block_identifier=block),
        head["timestamp"],
    )
    if v + vm <= 0 or v + vm > ROUND_CAP:
        raise ValueError("Allocation must be positive and at most 25,000 USDC")
    if e.functions.allocated().call() + v + vm > TARGET:
        raise ValueError("Program budget exceeded")
    result = reconcile(client, arb, c)
    atomic_json(report_path, result)
    if result["stop_reached"]:
        raise ValueError("Program stop condition reached")
    if result["hold"]:
        raise ValueError(
            "Reconciliation holds this allocation; inspect report warnings and pending value"
        )
    exclusions = c.get("exclusions", {})
    vl = sorted_addresses(exclusions.get("votium", []))
    ml = sorted_addresses(exclusions.get("vote_market", [])) if vm else []
    if v and len(vl) > votium.functions.maxExclusions().call():
        raise ValueError("Votium exclusions are disabled or exceed its limit")
    fee = 0
    if vm:
        if a["convex_voter"] not in ml:
            raise ValueError(
                "VoteMarket must exclude the Convex proxy to prevent Convex overlap"
            )
        if not arb:
            raise ValueError("ARBITRUM_RPC_URL is required for VoteMarket")
        require_safe(arb, c["campaign_manager"])
        campaign = (
            1,
            a["gauge"],
            c["campaign_manager"],
            a["usdc"],
            2,
            cap,
            vm,
            ml,
            ZERO,
            False,
        )
        fee = campaign_fee(client, a["remote"], e.address, a["market"], campaign, block)
        if fee > MAX_BRIDGE_FEE:
            raise ValueError("Bridge fee exceeds 0.1 ETH")
    round_id = votium.functions.activeRound().call(block_identifier=block)
    data = e.functions.allocate(v, vm, cap, vl, ml)._encode_transaction_data()
    tx = {"from": c["management"], "to": e.address, "value": fee, "data": data}
    gas = client.eth.estimate_gas(tx, block_identifier=block)
    if client.eth.get_block(block)["hash"] != head["hash"]:
        raise ValueError("Simulation block reorganized")
    late = vm and round_start(round_id) - head["timestamp"] < 86400
    return safe_batch(
        e.address,
        data,
        fee,
        description=f"Votium round {round_id}: {v / 1e6:,.2f} Votium, {vm / 1e6:,.2f} VoteMarket USDC",
        simulationBlock=block,
        simulationBlockHash=Web3.to_hex(head["hash"]),
        gasEstimate=gas,
        round=round_id,
        votiumFeeBps=votium.functions.platformFee().call(),
        timingWarning="Late posting: verify destination campaign start."
        if late
        else "",
    )


def campaign_action(arb, c, campaign_id, action):
    """Build Arbitrum Safe calls for settlement or recovery; never sign them."""
    require_safe(arb, c["campaign_manager"])
    market = contract(arb, "market", c["addresses"]["market"])
    head = arb.eth.get_block("latest")
    block = head["number"]
    cp = named(market.functions.campaignById(campaign_id), block)
    if cp["gauge"] != c["addresses"]["gauge"] or cp["manager"] != c["campaign_manager"]:
        raise ValueError(
            "Campaign does not belong to the configured gauge and management multisig"
        )
    if action == "close":
        if head["timestamp"] >= cp["endTimestamp"] + 28 * WEEK:
            raise ValueError(
                "Recovery window expired; closing would forfeit unused rewards"
            )
        data = market.functions.closeCampaign(campaign_id)._encode_transaction_data()
        arb.eth.call(
            {"from": c["campaign_manager"], "to": market.address, "data": data},
            block_identifier=block,
        )
        transactions = safe_batch(market.address, data)["transactions"]
    else:
        due = [
            week
            for week in range(cp["startTimestamp"], cp["endTimestamp"], WEEK)
            if week <= head["timestamp"]
        ]
        if not due:
            raise ValueError("Campaign has not started")
        vm_period(arb, market, campaign_id, due[-1], block)
        transactions = [
            safe_batch(
                market.address,
                market.functions.updateEpoch(
                    campaign_id, week, b""
                )._encode_transaction_data(),
            )["transactions"][0]
            for week in due
            if not named(market.functions.periodByCampaignId(campaign_id, week), block)[
                "updated"
            ]
        ]
        if not transactions:
            raise ValueError("All due campaign periods are already updated")
    result = safe_batch(
        market.address,
        "0x",
        chain_id=42161,
        name="Vyper campaign " + action,
        campaignId=campaign_id,
        safeAddress=c["campaign_manager"],
        simulationBlock=block,
        simulationBlockHash=Web3.to_hex(head["hash"]),
    )
    result["transactions"] = transactions
    return result


def doctor(client, arb, c):
    blockers = [
        k + " is not configured"
        for k in ["management", "campaign_manager"]
        if not c.get(k)
    ]
    if c.get("campaign_manager") and (
        not arb or not arb.eth.get_code(c["campaign_manager"])
    ):
        blockers.append(
            "Management Safe on Arbitrum is not deployed or RPC unavailable"
        )
    limit = contract(client, "votium").functions.maxExclusions().call()
    if len(c.get("exclusions", {}).get("votium", [])) > limit:
        blockers.append("Votium exclusions exceed live limit")
    if c.get("escrow"):
        verify_escrow(client, c)
    return {
        "blockers": blockers,
        "votium_max_exclusions": limit,
        "gauge_receiver": contract(client, "gauge").functions.receiver().call(),
    }


def main():
    p = argparse.ArgumentParser(description="Vyper grant operations")
    p.add_argument("--config", default="config/live.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("doctor")
    dp = sub.add_parser("deploy")
    dp.add_argument("--deployer", required=True)
    dp.add_argument("--out", default="output/deployment.json")
    plan = sub.add_parser("plan-round")
    plan.add_argument("--votium-usdc", required=True)
    plan.add_argument("--votemarket-usdc", required=True)
    plan.add_argument("--max-reward-per-vote", type=int, default=0)
    plan.add_argument("--out", default="output/allocation.safe.json")
    ca = sub.add_parser("campaign")
    ca.add_argument("action", choices=["update", "close"])
    ca.add_argument("--id", type=int, required=True)
    ca.add_argument("--out", default="output/campaign.safe.json")
    rc = sub.add_parser("reconcile")
    rc.add_argument("--out", default="output/report.json")
    rp = sub.add_parser("report")
    rp.add_argument("--input", default="output/report.json")
    rp.add_argument("--out", default="dashboard/dist")
    args = p.parse_args()
    try:
        if args.command == "build":
            atomic_json("build/escrow.json", artifact())
            print("Compiled contracts/VyperFundingEscrow.vy")
            return
        if args.command == "report":
            from .report import render

            render(json.loads(Path(args.input).read_text()), args.out)
            print(str(Path(args.out).resolve() / "index.html"))
            return
        c = load_config(args.config)
        client = rpc("ETHEREUM_RPC_URL", 1)
        arb = (
            rpc("ARBITRUM_RPC_URL", 42161)
            if os.environ.get("ARBITRUM_RPC_URL")
            else None
        )
        if args.command == "doctor":
            result = doctor(client, arb, c)
            print(json.dumps(result, indent=2))
            if result["blockers"]:
                raise SystemExit(1)
            return
        if args.command == "deploy":
            atomic_json(args.out, deployment(client, arb, c, args.deployer))
        elif not c.get("escrow"):
            raise ValueError("Deploy and configure escrow first")
        elif args.command == "plan-round":
            result = prepare(
                client,
                arb,
                c,
                usdc(args.votium_usdc),
                usdc(args.votemarket_usdc),
                args.max_reward_per_vote,
                "output/report.json",
            )
            atomic_json(args.out, result)
        elif args.command == "campaign":
            if not arb:
                raise ValueError("ARBITRUM_RPC_URL is required")
            atomic_json(args.out, campaign_action(arb, c, args.id, args.action))
        elif args.command == "reconcile":
            atomic_json(args.out, reconcile(client, arb, c))
        print(args.out)
    except Exception as error:
        # HTTP exceptions may contain authenticated RPC URLs. Never echo exception URLs.
        if (
            isinstance(error, (ValueError, FileNotFoundError))
            and "http" not in str(error).lower()
        ):
            message = str(error)
        else:
            message = (
                type(error).__name__
                + "; inspect the operation locally (connection details suppressed)"
            )
        print("Error: " + message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
