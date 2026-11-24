"""Dashboard data: the Vyper CRV claim call and the transaction list."""

import boa
import pytest
from eth.exceptions import Revert
from web3 import Web3
from conftest import UNIT
from vyper_grant import chain
from vyper_grant.chain import activity, votes, vyper_crv_claim, weekly_weights
from vyper_grant.core import ADDRESSES, artifact

WEEK_START = 1_795_046_400
SHARE = 25 * 10**16  # 25%


def test_claim_call_moves_all_held_crv_to_the_vyper_safe(system):
    s = system
    s.crv.mint(s.escrow.address, 112 * UNIT + 7)
    offline = Web3().eth.contract(address=s.escrow.address, abi=artifact("proceeds")["abi"])
    claim = vyper_crv_claim(offline, 112 * UNIT + 7)
    assert claim["safe"] == ADDRESSES["receiver"] == s.receiver
    assert claim["to"] == s.escrow.address
    calldata = bytes.fromhex(claim["data"][2:])

    with pytest.raises(Revert) as refused:
        boa.env.raw_call(claim["to"], sender=s.keeper, data=calldata)
    assert b"vyper" in refused.value.args[0]
    before = s.crv.balanceOf(s.receiver)
    boa.env.raw_call(claim["to"], sender=claim["safe"], data=calldata)
    assert s.crv.balanceOf(s.escrow.address) == 0
    assert s.crv.balanceOf(s.receiver) - before == 112 * UNIT + 7


def event(tx, block, index, **args):
    return {
        "transactionHash": bytes.fromhex(f"{tx:064x}"),
        "blockNumber": block,
        "logIndex": index,
        "args": args,
    }


def test_transactions_group_by_hash_newest_first():
    snapshots = [
        event(1, 10, 0, week=WEEK_START, yearn=1, total=4, manual=False),  # checkpoint
        event(2, 20, 5, week=WEEK_START, yearn=0, total=0, manual=True),  # manual weight
        event(3, 30, 1, week=WEEK_START, yearn=1, total=4, manual=False),  # inside work
    ]
    sales = [
        event(3, 30, 4, week=WEEK_START, crv=0, usdc=0, yearn=0,
              yearn_receiver=ADDRESSES["receiver"], vyper_crv=112 * UNIT),
        event(4, 30, 9, week=WEEK_START, crv=15_000 * UNIT, usdc=12_000 * 10**6,
              yearn=3_000 * 10**6, yearn_receiver=ADDRESSES["receiver"], vyper_crv=0),
    ]
    payments = [event(5, 25, 2, usdc=12_500 * 10**6, recipient=ADDRESSES["receiver"])]
    rows = activity(snapshots, sales, payments, {WEEK_START: SHARE}, lambda block: block * 12)

    assert [(r["tx"][-1], r["kind"]) for r in rows] == [
        ("4", "work"), ("3", "work"), ("5", "payment"), ("2", "checkpoint"), ("1", "checkpoint")
    ]
    sell, retain, payment, manual, checkpoint = rows
    assert sell["crv"] == 15_000 * UNIT
    assert (sell["yearn_usdc"], sell["vyper_usdc"], sell["vyper_crv"]) == (
        3_000 * 10**6, 9_000 * 10**6, 0)
    assert retain["crv"] == retain["vyper_crv"] == 112 * UNIT
    assert retain["share"] == SHARE and retain["timestamp"] == 360
    assert payment["usdc"] == 12_500 * 10**6
    assert payment["recipient"] == ADDRESSES["receiver"]
    assert manual["manual"] is True and manual["share"] == 0
    assert checkpoint["share"] == SHARE and checkpoint["manual"] is False



def test_transfers_out_each_get_a_row_with_their_recipient():
    """A Safe batch can pay, withdraw and recover in one transaction beside a work call."""
    snapshots = [event(7, 40, 0, week=WEEK_START, yearn=1, total=4, manual=False)]
    sales = [event(7, 40, 3, week=WEEK_START, crv=8 * UNIT, usdc=4 * 10**6, yearn=10**6,
                   yearn_receiver=ADDRESSES["receiver"], vyper_crv=0)]
    payments = [event(7, 40, 5, usdc=6_000 * 10**6, recipient=ADDRESSES["governance"])]
    withdrawals = [
        event(7, 40, 6, token=ADDRESSES["usdc"], amount=10**6, recipient=ADDRESSES["manager"]),
        event(7, 40, 7, token=ADDRESSES["crv"], amount=3 * UNIT, recipient=ADDRESSES["receiver"]),
    ]
    recoveries = [event(8, 41, 0, token=ADDRESSES["vault"], amount=5, receiver=ADDRESSES["governance"])]
    rows = activity(snapshots, sales, payments, {WEEK_START: SHARE}, lambda block: block * 12,
                    withdrawals, recoveries)

    assert [(r["kind"], r["index"]) for r in rows] == [
        ("recovery", 0), ("withdrawal", 7), ("withdrawal", 6), ("payment", 5), ("work", 0)
    ]
    recovery, crv, usdc, payment, work = rows
    assert (recovery["token"], recovery["amount"], recovery["recipient"]) == (
        ADDRESSES["vault"], 5, ADDRESSES["governance"])
    assert (crv["token"], crv["amount"], crv["recipient"]) == (
        ADDRESSES["crv"], 3 * UNIT, ADDRESSES["receiver"])
    assert usdc["recipient"] == ADDRESSES["manager"] and payment["recipient"] == ADDRESSES["governance"]
    assert work["yearn_usdc"] == 10**6 and {r["tx"] for r in rows[1:]} == {work["tx"]}


def test_funding_rounds_cancellations_and_recipient_changes_are_listed():
    rounds = [event(9, 50, 1, usdc=20_000 * 10**6, first_week=WEEK_START + 604800)]
    cancels = [event(10, 51, 0, usdc=10_000 * 10**6)]
    recipients = [event(11, 52, 3, recipient=ADDRESSES["manager"])]
    rows = activity([], [], [], {}, lambda block: block * 12, rounds=rounds, cancels=cancels,
                    recipients=recipients)

    changed, cancelled, funded = rows
    assert (changed["kind"], changed["recipient"]) == ("recipient", ADDRESSES["manager"])
    assert (cancelled["kind"], cancelled["usdc"]) == ("cancel", 10_000 * 10**6)
    assert (funded["kind"], funded["usdc"], funded["first_week"]) == (
        "round", 20_000 * 10**6, WEEK_START + 604800)


def test_weeks_carry_snapshot_weights_and_no_payment_data():
    later = WEEK_START + 604800
    rows = {WEEK_START: dict(week=WEEK_START), later: dict(week=later)}
    snapshots = [
        event(1, 10, 0, week=WEEK_START, yearn=3 * UNIT, total=12 * UNIT, manual=False),
        event(2, 11, 0, week=WEEK_START - 604800, yearn=1, total=2, manual=False),  # not reported
    ]
    weekly_weights(rows, snapshots)

    assert rows[WEEK_START] == dict(week=WEEK_START, yearn_weight=3 * UNIT, total_weight=12 * UNIT)
    assert rows[later] == dict(week=later, yearn_weight=None, total_weight=None)


class Reads:
    """A controller whose gauge nobody has touched this week unless `next_total` is set."""

    def __init__(self, next_total=0):
        self.weights = {WEEK_START: (1_000, 2), WEEK_START + 604800: (next_total, 0)}
        self.functions = self

    def answer(self, value):
        return type("Call", (), {"call": lambda _, block_identifier: value})()

    def vote_user_slopes(self, voter, gauge):
        return self.answer((3, 2_500, WEEK_START + 604800 + 100))

    def last_user_vote(self, voter, gauge):
        return self.answer(WEEK_START - 5)

    def gauge_relative_weight(self, gauge, at):
        assert at == WEEK_START
        return self.answer(15 * 10**15)

    def points_weight(self, gauge, at):
        return self.answer(self.weights[at])


@pytest.mark.parametrize(("next_total", "expected"), [(900, 900), (0, 1_000 - 2 * 604800)])
def test_votes_report_next_weeks_weights(monkeypatch, next_total, expected):
    monkeypatch.setattr(chain, "contract", lambda client, name: Reads(next_total))
    standing = votes(None, 7, WEEK_START + 3600)

    assert standing["relative_weight"] == 15 * 10**15 and standing["power"] == 2_500
    assert standing["last_vote"] == WEEK_START - 5
    total = max(expected, 0)
    assert standing["next"] == dict(
        week=WEEK_START + 604800, yearn_weight=min(300, total), total_weight=total)
