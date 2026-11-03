"""Rebuild reports from finalized chain evidence; no mutable financial ledger.

Missing evidence (`Pending`) leaves a week pending and holds planning. Any other
`ValueError` is a broken invariant and fails the whole reconciliation.
"""

from __future__ import annotations
from fractions import Fraction
from eth_abi import decode
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.logs import DISCARD
from .core import (
    TARGET,
    WEEK,
    ZERO,
    Pending,
    address,
    call,
    contract,
    credit,
    multicall,
    named,
    sorted_addresses,
    value_for_week,
)
from .chain import Chain, verify_escrow

ARBITRUM = 42161
RECOVERY_DELAY = 24 * WEEK
RECOVERY_WINDOW = 4 * WEEK


def serialized(data):
    if isinstance(data, dict):
        return {k: serialized(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [serialized(v) for v in data]
    if isinstance(data, int) and abs(data) > 2**53 - 1:
        return str(data)
    return data


def round_start(round_id):
    """Votium round vote start; its incentives cover the two following gauge weeks."""
    return (round_id + 1348) * 2 * WEEK


def evidence(log):
    return {
        "tx": Web3.to_hex(log["transactionHash"]),
        "block": log["blockNumber"],
        "block_hash": Web3.to_hex(log["blockHash"]),
    }


def token_received(token, receipt, sender, recipient):
    return sum(
        x["args"]["value"]
        for x in token.events.Transfer().process_receipt(receipt, errors=DISCARD)
        if x["args"]["from"] == sender and x["args"]["to"] == recipient
    )


# ---------------------------------------------------------------------------
# Allocations (Ethereum)


def allocation(ch, e, log, report):
    """One Allocated event, cross-checked against its Votium deposit / LaPoste message."""
    c, client = ch.config, ch.client
    args = log["args"]
    a = {
        "round": args["round"],
        "votium_amount": args["votium_amount"],
        "vote_market_amount": args["vote_market_amount"],
        "max_reward_per_vote": args["max_reward_per_vote"],
        "exclusions": {
            "votium": list(args["votium_exclusions"]),
            "vote_market": list(args["vote_market_exclusions"]),
        },
        **evidence(log),
        "timestamp": ch.block(log["blockNumber"])["timestamp"],
    }
    receipt = client.eth.get_transaction_receipt(log["transactionHash"])
    if receipt["blockHash"] != log["blockHash"] or receipt["status"] != 1:
        raise ValueError("Allocation receipt changed")
    if a["votium_amount"]:
        deposits = [
            d["args"]
            for d in ch.contracts["votium"]
            .events.NewIncentive()
            .process_receipt(receipt, errors=DISCARD)
            if d["args"]["_depositor"] == e.address and not d["args"]["_recycled"]
        ]
        if len(deposits) != 1:
            raise ValueError("Missing unique Votium deposit")
        d = deposits[0]
        if (
            d["_round"] != a["round"]
            or d["_gauge"] != c["addresses"]["gauge"]
            or list(d["_excluded"]) != a["exclusions"]["votium"]
        ):
            raise ValueError("Votium deposit differs from allocation")
        a["votium_incentive"] = d["_index"]
        report["cost_paid_usdc"] += a["votium_amount"] - d["_amount"]  # platform fee
    if a["vote_market_amount"]:
        laposte = call(
            client,
            c["addresses"]["remote"],
            "LA_POSTE",
            outputs=("address",),
            block=log["blockNumber"],
        )
        messages = [
            m["args"]
            for m in contract(client, "laposte", laposte)
            .events.MessageSent()
            .process_receipt(receipt, errors=DISCARD)
            if m["args"]["sender"] == c["addresses"]["remote"]
            and m["args"]["chainId"] == ARBITRUM
        ]
        if len(messages) != 1:
            raise ValueError("Missing unique LaPoste source message")
        payload = messages[0]["message"]["payload"]
        action, origin, platform, _ = decode(
            ["(uint8,address,address,bytes)"], payload
        )[0]
        if (
            action != 0
            or address(origin) != e.address
            or address(platform) != c["addresses"]["market"]
        ):
            raise ValueError("Unexpected remote campaign origin")
        a["laposte"] = laposte
        a["laposte_nonce"] = messages[0]["nonce"]
        a["payload_hash"] = Web3.to_hex(Web3.keccak(payload))
    return a


# ---------------------------------------------------------------------------
# Votium (Ethereum)


def votium(ch, e, start, report, coverage):
    """Follow every Votium incentive from the escrow, including automatic recycling."""
    c = ch.config
    gauge, usdc = c["addresses"]["gauge"], c["addresses"]["usdc"]
    deposits = ch.logs(
        ch.contracts["votium"].events.NewIncentive(),
        start,
        filters={"_depositor": e.address, "_gauge": gauge},
    )
    if not deposits:
        return
    refunds = {
        (x["args"]["_round"], x["args"]["_index"]): x
        for x in ch.logs(ch.contracts["votium"].events.WithdrawUnprocessed(), start)
        if x["args"]["_gauge"] == gauge
    }
    for event in deposits:
        d = event["args"]
        if d["_token"] != usdc:
            continue
        inc = named(
            ch.contracts["votium"].functions.viewIncentive(
                d["_round"], gauge, d["_index"]
            ),
            ch.number,
        )
        report["cost_paid_usdc"] += inc["distributed"]
        report["committed_usdc"] += max(
            0, inc["amount"] - inc["distributed"] - inc["recycled"]
        )
        refund = refunds.get((d["_round"], d["_index"]))
        if refund:
            receipt = ch.client.eth.get_transaction_receipt(refund["transactionHash"])
            actual = token_received(
                ch.contracts["usdc"], receipt, c["addresses"]["votium"], e.address
            )
            if actual != refund["args"]["_amount"]:
                raise ValueError("Votium refund token transfer mismatch")
            report["refunds_usdc"] += actual
        # A recycled zero-vote round bought no emissions; the child incentive covers it.
        if inc["distributed"] or (inc["amount"] and not inc["recycled"]):
            source = {
                "round": d["_round"],
                "exclusions": list(d["_excluded"]),
                "tx": Web3.to_hex(event["transactionHash"]),
            }
            first = round_start(d["_round"]) + WEEK
            for week in [first, first + WEEK]:
                coverage.setdefault(week, {})["votium"] = source


def convex_share(ch, round_id, excluded):
    """Excluded and total Votium votes for a round, cross-checked against Convex."""
    c = ch.config
    g = c["addresses"]["gauge"]
    matches = []
    for platform in c.get("convex_voting_history", [c["addresses"]["convex"]]):
        cv = contract(ch.client, "convex", platform)
        count = cv.functions.proposalCount().call(block_identifier=ch.number)
        for pid in reversed(range(count)):
            p = named(cv.functions.proposals(pid), ch.number)
            if abs(p["startTime"] - round_start(round_id)) < 86400:
                if not cv.functions.isFinalized(pid).call(block_identifier=ch.number):
                    raise Pending("Convex proposal not finalized")
                matches.append((cv, pid))
    if len(matches) != 1:
        raise ValueError("Cannot uniquely identify the finalized Convex proposal")
    cv, pid = matches[0]
    total = cv.functions.gaugeTotal(pid, g).call(block_identifier=ch.number)
    submitted = ch.read("votium", "votesReceived", round_id, g)
    if total == 0 or submitted == 0:
        raise Pending("Missing Convex/Votium gauge totals")
    # Votium reports votes on its own power-of-ten scale relative to Convex.
    ratio = Fraction(submitted, total)
    scale = min(
        (Fraction(10) ** p for p in range(-24, 25)), key=lambda x: abs(x - ratio)
    )
    if abs(scale - ratio) / scale > Fraction(1, 10000):
        raise ValueError("Convex and Votium totals disagree")
    black = sum(
        ch.read("votium", "excludedVotesReceived", round_id, g, a) for a in excluded
    )
    # Directly visible excluded votes must be within Votium's published exclusions.
    direct = 0
    for account in excluded:
        v = named(cv.functions.getVote(pid, account), ch.number)
        if v["voted"]:
            power = v["baseWeight"] + v["adjustedWeight"]
            if power < 0:
                raise ValueError("Negative finalized Convex user weight")
            direct += sum(
                power * w // 1_000_000
                for gauge, w in zip(v["gauges"], v["weights"])
                if gauge == g
            )
    if Fraction(direct) * scale - black > max(
        Fraction(10) * scale, Fraction(submitted, 10000)
    ):
        raise ValueError("Direct Convex votes are missing from Votium exclusions")
    return (
        black,
        submitted,
        {
            "platform": cv.address,
            "proposal": pid,
            "convex_total": total,
            "votium_total": submitted,
            "excluded_total": black,
        },
    )


# ---------------------------------------------------------------------------
# VoteMarket (Arbitrum destination)


def vm_period(client, market, campaign, week, block):
    """Prove settlement is available without requiring a voter to have claimed yet."""
    try:
        cp = named(market.functions.campaignById(campaign), block)
        period = named(market.functions.periodByCampaignId(campaign, week), block)
        if not period["updated"]:
            calls = [
                market.functions.updateEpoch(campaign, previous, b"")
                for previous in range(cp["startTimestamp"], week + WEEK, WEEK)
                if not named(
                    market.functions.periodByCampaignId(campaign, previous), block
                )["updated"]
            ]
            calls.append(market.functions.periodByCampaignId(campaign, week))
            values = decode(
                ["uint256", "uint256", "uint256", "bool"],
                multicall(client, calls, block)[-1],
            )
            period = dict(zip(period, values))
        return period
    except ContractLogicError as error:
        raise Pending("VoteMarket oracle settlement is not available yet") from error


def delivered(ar, market, log, a, c):
    """True when a CampaignCreated log is the authenticated delivery of allocation a."""
    d = log["args"]
    if d["gauge"] != c["addresses"]["gauge"] or d["manager"] != c["campaign_manager"]:
        return False
    if (
        d["totalRewardAmount"] != a["vote_market_amount"]
        or d["maxRewardPerVote"] != a["max_reward_per_vote"]
        or ar.block(log["blockNumber"])["timestamp"] < a["timestamp"]
    ):
        return False
    receipt = ar.client.eth.get_transaction_receipt(log["transactionHash"])
    deliveries = [
        x["args"]
        for x in contract(ar.client, "laposte", a["laposte"])
        .events.MessageReceived()
        .process_receipt(receipt, errors=DISCARD)
        if x["args"]["chainId"] == 1
        and x["args"]["nonce"] == a["laposte_nonce"]
        and x["args"]["sender"] == c["addresses"]["remote"]
        and x["args"]["success"]
        and Web3.to_hex(Web3.keccak(x["args"]["message"]["payload"]))
        == a["payload_hash"]
    ]
    pid = d["campaignId"]
    return (
        len(deliveries) == 1
        and sorted_addresses(
            market.functions.getAddressesByCampaign(pid).call(
                block_identifier=ar.number
            )
        )
        == a["exclusions"]["vote_market"]
        and not market.functions.whitelistOnly(pid).call(block_identifier=ar.number)
    )


def campaign_row(ch, ar, market, log, report):
    """Destination campaign state, with refunds verified from token transfers."""
    c = ch.config
    pid = log["args"]["campaignId"]
    cp = named(market.functions.campaignById(pid), ar.number)
    factory = call(
        ch.client, c["addresses"]["remote"], "TOKEN_FACTORY", outputs=("address",)
    )
    wrapped = call(
        ar.client,
        factory,
        "wrappedTokens",
        ("address",),
        ("address",),
        (c["addresses"]["usdc"],),
        ar.number,
    )
    if (
        cp["chainId"] != 1
        or cp["rewardToken"] != wrapped
        or cp["numberOfPeriods"] != 2
        or cp["hook"] != ZERO
    ):
        raise ValueError("Unexpected destination campaign")
    row = {
        "id": pid,
        **evidence(log),
        "start": cp["startTimestamp"],
        "end": cp["endTimestamp"],
        "amount": cp["totalRewardAmount"],
        "paid": market.functions.totalClaimedByCampaignId(pid).call(
            block_identifier=ar.number
        ),
        "closed": market.functions.isClosedCampaign(pid).call(
            block_identifier=ar.number
        ),
        "recovery_start": cp["endTimestamp"] + RECOVERY_DELAY,
        "recovery_end": cp["endTimestamp"] + RECOVERY_DELAY + RECOVERY_WINDOW,
        "wrapped_token": wrapped,
        "manager": cp["manager"],
        "cancelled": False,
    }
    if not row["closed"]:
        report["committed_usdc"] += row["amount"] - row["paid"]
        report["cost_paid_usdc"] += row["paid"]
        return row
    closures = [
        x
        for x in ar.logs(market.events.CampaignClosed(), log["blockNumber"])
        if x["args"]["campaignId"] == pid
    ]
    if len(closures) != 1:
        raise ValueError("Missing unique campaign close receipt")
    receipt = ar.client.eth.get_transaction_receipt(closures[0]["transactionHash"])
    refund = token_received(
        contract(ar.client, "usdc", wrapped), receipt, market.address, cp["manager"]
    )
    # closeCampaign sets totalClaimed to the ENTIRE deposit, including the refund.
    row.update(refund=refund, paid=row["amount"] - refund)
    row["cancelled"] = ar.block(closures[0]["blockNumber"])["timestamp"] < row["start"]
    report["refunds_usdc"] += refund
    report["cost_paid_usdc"] += row["paid"]
    if refund == 0 and row["amount"] > 0:
        report["warnings"].append(
            f"Campaign {pid}: verify whether unused funds were forfeited at closure."
        )
    return row


def votemarket(ch, arb, report, requests, coverage):
    """Match each Ethereum submission to exactly one authenticated Arbitrum campaign."""
    c = ch.config
    if arb is None or c.get("arb_scan_from_block") is None:
        report["warnings"].append(
            "Arbitrum scan is required to confirm VoteMarket delivery."
        )
        report["committed_usdc"] += sum(a["vote_market_amount"] for a in requests)
        return None
    ar = Chain(arb, c)
    market = contract(arb, "market", c["addresses"]["market"])
    ar.contracts = {"market": market}
    report["arb_source_block"] = ar.number
    report["arb_source_hash"] = Web3.to_hex(ar.final["hash"])
    created = ar.logs(market.events.CampaignCreated(), int(c["arb_scan_from_block"]))
    used = set()
    for a in requests:
        candidates = [
            log
            for log in created
            if log["args"]["campaignId"] not in used
            and delivered(ar, market, log, a, c)
        ]
        if len(candidates) != 1:
            report["warnings"].append(
                f"VoteMarket {a['tx']}: delivery missing or ambiguous; no credit assigned."
            )
            report["committed_usdc"] += a["vote_market_amount"]
            continue
        row = campaign_row(ch, ar, market, candidates[0], report)
        used.add(row["id"])
        report["campaigns"].append(row)
        a["campaign"] = row
        if not row["cancelled"]:
            for week in range(row["start"], row["end"], WEEK):
                coverage.setdefault(week, {}).setdefault("vm", []).append(a)
    return market


# ---------------------------------------------------------------------------
# Weekly value


def running_week(ch, week, row, report):
    """Partial week: report accrual so far plus an upper bound on what remains."""
    c = ch.config
    receiver, gauge = c["addresses"]["receiver"], c["addresses"]["gauge"]
    opening = ch.snapshot(week)
    accrued = ch.read("minter", "minted", receiver, gauge) + ch.read(
        "gauge", "claimable_tokens_write", receiver
    )
    # The current rate only falls at annual reductions; relative weight is fixed per week.
    rate = ch.read("gauge", "inflation_rate")
    relative = ch.read("controller", "gauge_relative_weight", gauge, week)
    future = rate * relative * (week + WEEK - ch.final["timestamp"]) // 10**18
    future = min(future, max(0, ch.read("gauge", "max_emissions") - accrued))
    row.update(
        opening=opening,
        closing={
            "accrued_crv": accrued,
            "block": ch.number,
            "block_hash": Web3.to_hex(ch.final["hash"]),
        },
        price=opening["price"],
        gross_crv=accrued - opening["accrued_crv"],
        remaining_crv_upper=future,
        reasons=["Emission week is still running"],
    )
    report["pending_upper_usdc"] += (
        (row["gross_crv"] + future) * opening["price"] // 10**20
    )


def vm_weight(ch, arb, market, report, week, sources, total, row):
    """Curve weight eligible through settled VoteMarket campaigns for the week."""
    c = ch.config
    settled = []
    for a in sources.get("vm", []):
        period = vm_period(
            arb, market, a["campaign"]["id"], week, report["arb_source_block"]
        )
        row.setdefault("votemarket_periods", []).append(
            {
                "campaign": a["campaign"]["id"],
                "reward_per_vote": period["rewardPerVote"],
                "updated": period["updated"],
            }
        )
        if period["rewardPerVote"] > 0:
            settled.append(a)
    if not settled:
        return 0
    excluded = sorted(
        set.intersection(*[set(a["exclusions"]["vote_market"]) for a in settled])
    )
    if "votium" in sources and c["addresses"]["convex_voter"] not in excluded:
        raise ValueError("Convex proxy overlaps both markets")
    oracle = market.functions.ORACLE().call(block_identifier=report["arb_source_block"])
    oracle_total = call(
        arb,
        oracle,
        "getTotalVotes",
        ("address", "uint256"),
        ("uint256",),
        (c["addresses"]["gauge"], week),
        report["arb_source_block"],
    )
    if oracle_total != total:
        raise ValueError("VoteMarket oracle and Curve weekly weights disagree")
    return total - sum(ch.voter_weight(a, week) for a in excluded)


def check_emissions(ch, week, opening, closing, row):
    """Cross-check measured accrual against rate x relative weight x one week.

    Skipped across an annual rate cut or a kill, which the accrual measure handles
    but the closed form does not. Tolerance covers the boundary blocks landing a few
    seconds after 00:00 UTC (relative weight is at most 1).
    """
    if (
        opening["future_epoch_time"] < week + WEEK
        or opening["killed"]
        or closing["killed"]
    ):
        row["emissions_check"] = "skipped"
        return
    rate = opening["inflation_rate"]
    expected = rate * opening["relative_weight"] * WEEK // 10**18
    cap = ch.read("gauge", "max_emissions", block=closing["block"])
    expected = min(expected, max(0, cap - opening["accrued_crv"]))
    drift = (opening["timestamp"] - week) + (closing["timestamp"] - week - WEEK)
    if abs(row["gross_crv"] - expected) > rate * drift + 10**6:
        raise ValueError(f"Week {week}: accrued CRV disagrees with gauge emission rate")
    row["emissions_check"] = str(expected)


def settled_week(ch, arb, market, report, week, sources, minted, row):
    c = ch.config
    opening, closing = ch.snapshot(week), ch.snapshot(week + WEEK)
    row.update(opening=opening, closing=closing, price=opening["price"])
    row["gross_crv"] = closing["accrued_crv"] - opening["accrued_crv"]
    check_emissions(ch, week, opening, closing, row)
    total = opening["total_weight"]
    vm = vm_weight(ch, arb, market, report, week, sources, total, row)
    cv = black = cvtotal = 0
    if "votium" in sources:
        source = sources["votium"]
        if ch.read("votium", "lastRoundProcessed") < source["round"]:
            raise Pending("Votium result not finalized")
        cv = ch.voter_weight(c["addresses"]["convex_voter"], week)
        black, cvtotal, row["convex_evidence"] = convex_share(
            ch, source["round"], source["exclusions"]
        )
    ratio = credit(total, vm, cv, black, cvtotal)
    row.update(
        value_for_week(
            opening["accrued_crv"],
            closing["accrued_crv"],
            minted,
            ratio,
            opening["price"],
        ),
        status="settled",
        ratio_numerator=str(ratio.numerator),
        ratio_denominator=str(ratio.denominator),
        vm_weight=str(vm),
        convex_weight=str(cv),
    )
    report["received_value_usdc"] += row["received_value_usdc"]
    report["pending_upper_usdc"] += max(
        0, row["accrued_value_usdc"] - row["received_value_usdc"]
    )


def weeks(ch, arb, market, report, coverage):
    c = ch.config
    now = ch.final["timestamp"]
    minted = ch.read(
        "minter", "minted", c["addresses"]["receiver"], c["addresses"]["gauge"]
    )
    for week, sources in sorted(coverage.items()):
        if week > now:
            continue
        row = {"week": week, "status": "pending", "reasons": []}
        try:
            if week + WEEK > now:
                running_week(ch, week, row, report)
            else:
                settled_week(ch, arb, market, report, week, sources, minted, row)
        except Pending as error:
            row["reasons"].append(str(error))
            if "gross_crv" in row:
                report["pending_upper_usdc"] += (
                    row["gross_crv"] * row["price"] // 10**20
                )
            else:
                report["warnings"].append(
                    f"Week {week}: accounting data unavailable; planning is held."
                )
        report["weeks"].append(row)
    report["unfinished_weeks"] = sum(1 for w in coverage if w + WEEK > now)


# ---------------------------------------------------------------------------


def reconcile(client, arb, c):
    if c.get("deployment_block") is None:
        raise ValueError("deployment_block is required")
    start = int(c["deployment_block"])
    ch = Chain(client, c)
    e = ch.contracts["escrow"] = verify_escrow(client, c)
    report = {
        "chain_id": 1,
        "escrow": e.address,
        "source_block": ch.number,
        "source_hash": Web3.to_hex(ch.final["hash"]),
        "as_of": ch.final["timestamp"],
        "target_usdc": TARGET,
        "funded_usdc": sum(
            x["args"]["value"]
            for x in ch.logs(
                ch.contracts["usdc"].events.Transfer(), start, filters={"to": e.address}
            )
            if x["args"]["from"] != c["addresses"]["votium"]
        ),
        "recalled_usdc": sum(
            x["args"]["amount"] for x in ch.logs(e.events.Recalled(), start)
        ),
        "allocated_usdc": e.functions.allocated().call(block_identifier=ch.number),
        "balance_usdc": ch.read("usdc", "balanceOf", e.address),
        "cost_paid_usdc": 0,
        "committed_usdc": 0,
        "refunds_usdc": 0,
        "received_value_usdc": 0,
        "pending_upper_usdc": 0,
        "weeks": [],
        "allocations": [],
        "campaigns": [],
        "warnings": [],
    }
    report["allocations"] = [
        allocation(ch, e, log, report) for log in ch.logs(e.events.Allocated(), start)
    ]
    coverage = {}
    votium(ch, e, start, report, coverage)
    requests = [a for a in report["allocations"] if a["vote_market_amount"]]
    market = votemarket(ch, arb, report, requests, coverage) if requests else None
    weeks(ch, arb, market, report, coverage)
    report["delivery_pending"] = len(requests) - len(report["campaigns"])
    report["stop_reached"] = (
        report["received_value_usdc"] >= TARGET or report["allocated_usdc"] >= TARGET
    )
    report["hold"] = bool(
        report["warnings"]
        or report["delivery_pending"]
        or report["received_value_usdc"] + report["pending_upper_usdc"] >= TARGET
    )
    if client.eth.get_block(ch.number)["hash"] != ch.final["hash"]:
        raise ValueError("Source block reorganized; rerun reconciliation")
    if (
        report.get("arb_source_block") is not None
        and Web3.to_hex(arb.eth.get_block(report["arb_source_block"])["hash"])
        != report["arb_source_hash"]
    ):
        raise ValueError("Arbitrum source block reorganized")
    return serialized(report)
