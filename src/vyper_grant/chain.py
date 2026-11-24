"""Deployment verification, historical recovery, and event reports."""

from web3 import Web3
from .core import ADDRESSES, WEEK, artifact, contract, named


def verify(client, c, block="latest", names=("funding", "proceeds")):
    required = set(names)
    if any(c.get(k) is None for k in required):
        raise ValueError("Missing configuration: " + ", ".join(sorted(required)))
    contracts = {n: contract(client, n, c.get(n)) for n in names}
    expected = {"funding": {}}
    if "proceeds" in names:
        expected["proceeds"] = {
            "RECEIVER": ADDRESSES["receiver"],
        }
    for name, instance in contracts.items():
        code = bytes(client.eth.get_code(instance.address, block_identifier=block))
        runtime = bytes.fromhex(artifact(name)["bytecode_runtime"][2:])
        # Fixed addresses are embedded in the verified runtime.
        if not code.startswith(runtime):
            raise ValueError(f"Unexpected {name} bytecode")
        for getter, value in expected[name].items():
            if instance.functions[getter]().call(block_identifier=block) != value:
                raise ValueError(f"Unexpected {name} {getter}")
    return tuple(contracts[n] for n in names)


def logs(client, event, start, end):
    return [
        log
        for first in range(start, end + 1, 2000)
        for log in event.get_logs(from_block=first, to_block=min(first + 1999, end))
    ]


def historical_vote(client, week, final):
    """Read just BEFORE the boundary block; a boundary-block revote is for next week."""
    if week > final["timestamp"]:
        raise ValueError("Boundary is not finalized")
    low, high = 0, final["number"]
    while low < high:
        middle = (low + high) // 2
        if client.eth.get_block(middle)["timestamp"] < week:
            low = middle + 1
        else:
            high = middle
    block = client.eth.get_block(low - 1)
    ctrl = contract(client, "controller").functions
    slope, _, end = ctrl.vote_user_slopes(ADDRESSES["voter"], ADDRESSES["gauge"]).call(
        block_identifier=block["number"]
    )
    voted = ctrl.last_user_vote(ADDRESSES["voter"], ADDRESSES["gauge"]).call(
        block_identifier=block["number"]
    )
    return {
        "week": week,
        "slope": slope,
        "end": end,
        "last_vote": voted,
        "block": block["number"],
        "block_hash": Web3.to_hex(block["hash"]),
    }


def report(client, c, *, head=None):
    """Read everything at one block: the latest mined block unless `head` is given."""
    if head is None:
        head = client.eth.get_block("latest")
    block = head["number"]
    if c.get("deployment_block") is None or not 0 <= c["deployment_block"] <= block:
        raise ValueError("deployment_block must be within the report block range")
    funding, proceeds = [contract(client, n, c[n]) for n in ["funding", "proceeds"]]
    def read(fn):
        return fn.call(block_identifier=block)

    def events(event):
        return logs(client, event, c["deployment_block"], block)

    times = {}

    def timestamp(number):
        if number not in times:
            times[number] = client.eth.get_block(number)["timestamp"]
        return times[number]

    drawn = events(funding.events.Drawn())
    payments = [
        dict(
            usdc=e["args"]["usdc"],
            recipient=e["args"]["recipient"],
            timestamp=timestamp(e["blockNumber"]),
            tx=Web3.to_hex(e["transactionHash"]),
        )
        for e in drawn
    ]
    start = read(proceeds.functions.DEPLOYED_WEEK())
    rows = {
        week: dict(week=week, sold=0, output=0, yearn=0, vyper_crv=0)
        for week in range(start, head["timestamp"] + 1, WEEK)
    }
    forwarded = 0
    sales = events(proceeds.events.Sold())
    for event in sales:
        a = event["args"]
        rows.setdefault(
            a["week"], dict(week=a["week"], sold=0, output=0, yearn=0, vyper_crv=0)
        )
        if a["yearn_receiver"] != proceeds.address:
            forwarded += a["yearn"]
        if a["yearn"] > a["usdc"]:
            raise ValueError("Sale conservation mismatch")
        for key, field in [
            ("sold", "crv"),
            ("output", "usdc"),
            ("yearn", "yearn"),
            ("vyper_crv", "vyper_crv"),
        ]:
            rows[a["week"]][key] += a[field]
    warnings = []
    for week, row in rows.items():
        row.update(named(proceeds.functions.weeks(week), block))
        row["active"] = True
        current = week <= head["timestamp"] < week + WEEK
        missing = current and not row["ready"]
        row["status"] = (
            "Needs checkpoint"
            if missing
            else "Active"
            if current
            else "Closed"
        )
        if missing:
            warnings.append("The current week needs a vote snapshot")
    if read(proceeds.functions.paused()):
        warnings.append("Sales are paused; public checkpoint remains available")
    withdrawals = events(proceeds.events.Withdrawn())
    vyper_crv = read(
        contract(client, "token", ADDRESSES["crv"]).functions.balanceOf(proceeds.address)
    )
    shares = {week: row["share"] for week, row in rows.items()}
    data = dict(
        as_of=head["timestamp"],
        block=block,
        block_hash=Web3.to_hex(head["hash"]),
        funding=funding.address,
        funding_recipient=read(funding.functions.recipient()),
        funding_next_week=read(funding.functions.next_week()),
        funding_payments=[read(funding.functions.payments(i)) for i in range(2)],
        proceeds=proceeds.address,
        start=start,
        yearn_receiver=read(proceeds.functions.yearn_receiver()),
        forwarded_usdc=forwarded,
        payments=payments,
        spent=sum(p["usdc"] for p in payments),
        funding_assets=read(funding.functions.assets()),
        proceeds_balance=read(
            contract(client, "token", ADDRESSES["usdc"]).functions.balanceOf(
                proceeds.address
            )
        ),
        vyper_crv_balance=vyper_crv,
        vyper_crv_claim=vyper_crv_claim(proceeds, vyper_crv),
        transactions=activity(
            events(proceeds.events.Snapshot()), sales, drawn, shares, timestamp
        ),
        totals={
            k: sum(r[k] for r in rows.values()) for k in ["sold", "output", "yearn", "vyper_crv"]
        },
        weeks=sorted(rows.values(), key=lambda r: r["week"]),
        withdrawn_usdc=sum(
            e["args"]["amount"]
            for e in withdrawals
            if e["args"]["token"] == ADDRESSES["usdc"]
        ),
        withdrawn_crv=sum(
            e["args"]["amount"]
            for e in withdrawals
            if e["args"]["token"] == ADDRESSES["crv"]
        ),
        warnings=list(dict.fromkeys(warnings)),
    )
    if client.eth.get_block(block)["hash"] != head["hash"]:
        raise ValueError("Source block reorganized; rerun")
    return serialize(data)


def vyper_crv_claim(proceeds, amount):
    """The Vyper Safe's call that withdraws all held CRV to itself."""
    safe = ADDRESSES["receiver"]
    call = proceeds.functions.withdraw(ADDRESSES["crv"], amount, safe)
    return dict(safe=safe, to=proceeds.address, data=call._encode_transaction_data())


def activity(snapshots, sales, payments, shares, timestamp):
    """One row per checkpoint, work or funding payment transaction, newest first.

    A work call that records the week's share also emits Snapshot; it is
    shown once, as work. CRV withdrawals are not listed.
    """
    rows = {}

    def row(event):
        tx = Web3.to_hex(event["transactionHash"])
        if tx not in rows:
            rows[tx] = dict(
                tx=tx,
                block=event["blockNumber"],
                index=event["logIndex"],
                timestamp=timestamp(event["blockNumber"]),
            )
        return rows[tx]

    for e in snapshots:
        a = e["args"]
        r = row(e)
        r.setdefault("kind", "checkpoint")
        r.update(
            week=a["week"],
            share=a["yearn"] * 10**18 // a["total"] if a["total"] else 0,
            manual=a["manual"],
        )
    for e in sales:
        a = e["args"]
        r = row(e)
        r.update(
            kind="work",
            week=a["week"],
            share=shares[a["week"]],
            crv=a["crv"] + a["vyper_crv"],
            yearn_usdc=a["yearn"],
            vyper_usdc=a["usdc"] - a["yearn"],
            vyper_crv=a["vyper_crv"],
        )
    for e in payments:
        r = row(e)
        r.update(kind="payment", usdc=e["args"]["usdc"])
    return sorted(rows.values(), key=lambda r: (r["block"], r["index"]), reverse=True)


def serialize(value):
    """JSON/JavaScript must not round 18-decimal token quantities."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value
