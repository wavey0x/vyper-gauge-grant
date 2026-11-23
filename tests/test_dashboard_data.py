"""Dashboard data: the Vyper CRV claim call and the transaction list."""

import boa
import pytest
from eth.exceptions import Revert
from web3 import Web3
from conftest import UNIT
from vyper_grant.chain import activity, vyper_crv_claim
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
    assert manual["manual"] is True and manual["share"] == 0
    assert checkpoint["share"] == SHARE and checkpoint["manual"] is False
