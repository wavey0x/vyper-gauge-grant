"""Deployment verification, historical recovery, and finalized event reports."""

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


def report(client, c):
    final = client.eth.get_block("finalized")
    block = final["number"]
    if c.get("deployment_block") is None or not 0 <= c["deployment_block"] <= block:
        raise ValueError("A finalized deployment_block is required")
    funding, proceeds = [contract(client, n, c[n]) for n in ["funding", "proceeds"]]
    def read(fn):
        return fn.call(block_identifier=block)

    def events(event):
        return logs(client, event, c["deployment_block"], block)
    payments = [
        dict(
            usdc=e["args"]["usdc"],
            recipient=e["args"]["recipient"],
            timestamp=client.eth.get_block(e["blockNumber"])["timestamp"],
            tx=Web3.to_hex(e["transactionHash"]),
        )
        for e in events(funding.events.Drawn())
    ]
    start = read(proceeds.functions.DEPLOYED_WEEK())
    rows = {
        week: dict(week=week, sold=0, output=0, yearn=0)
        for week in range(start, final["timestamp"] + 1, WEEK)
    }
    forwarded = 0
    for event in events(proceeds.events.Sold()):
        a = event["args"]
        rows.setdefault(
            a["week"], dict(week=a["week"], sold=0, output=0, yearn=0)
        )
        if a["yearn_receiver"] != proceeds.address:
            forwarded += a["yearn"]
        if a["yearn"] > a["crvusd"]:
            raise ValueError("Sale conservation mismatch")
        for key, field in [
            ("sold", "crv"),
            ("output", "crvusd"),
            ("yearn", "yearn"),
        ]:
            rows[a["week"]][key] += a[field]
    warnings = []
    for week, row in rows.items():
        row.update(named(proceeds.functions.weeks(week), block))
        row["active"] = True
        current = week <= final["timestamp"] < week + WEEK
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
    data = dict(
        as_of=final["timestamp"],
        block=block,
        block_hash=Web3.to_hex(final["hash"]),
        funding=funding.address,
        funding_recipient=read(funding.functions.recipient()),
        proceeds=proceeds.address,
        start=start,
        yearn_receiver=read(proceeds.functions.yearn_receiver()),
        forwarded_crvusd=forwarded,
        payments=payments,
        spent=sum(p["usdc"] for p in payments),
        funding_assets=read(funding.functions.assets()),
        proceeds_balance=read(
            contract(client, "token", ADDRESSES["crvusd"]).functions.balanceOf(
                proceeds.address
            )
        ),
        totals={
            k: sum(r[k] for r in rows.values()) for k in ["sold", "output", "yearn"]
        },
        weeks=sorted(rows.values(), key=lambda r: r["week"]),
        withdrawn_crvusd=sum(
            e["args"]["amount"]
            for e in events(proceeds.events.Withdrawn())
            if e["args"]["token"] == ADDRESSES["crvusd"]
        ),
        warnings=list(dict.fromkeys(warnings)),
    )
    if client.eth.get_block(block)["hash"] != final["hash"]:
        raise ValueError("Source block reorganized; rerun")
    return serialize(data)


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
