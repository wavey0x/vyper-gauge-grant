"""Pinned reads and an independently reconstructed event ledger."""

from web3 import Web3
from .core import ADDRESSES, BUDGET, WEEK, artifact, contract, named
from .accounting import weight


def verify(client, c, block="latest"):
    if any(
        c.get(k) is None for k in ["funding", "proceeds", "payee", "start", "weeks"]
    ):
        raise ValueError("Configure both contracts, payee, start and weeks first")
    contracts = {n: contract(client, n, c.get(n)) for n in ["funding", "proceeds"]}
    expected = {
        "funding": {
            "VAULT": ADDRESSES["vault"],
            "USDC": ADDRESSES["usdc"],
            "MANAGER": ADDRESSES["manager"],
            "PAYEE": c["payee"],
            "START": c["start"],
            "END": c["start"] + c["weeks"] * WEEK,
        },
        "proceeds": {
            k.upper(): v
            for k, v in ADDRESSES.items()
            if k
            in [
                "manager",
                "crv",
                "crvusd",
                "gauge",
                "controller",
                "minter",
                "voter",
                "receiver",
                "pool",
            ]
        },
    }
    expected["proceeds"].update(
        FUNDING=c["funding"], START=c["start"], END=expected["funding"]["END"]
    )
    for name, instance in contracts.items():
        code = bytes(client.eth.get_code(instance.address, block_identifier=block))
        runtime = bytes.fromhex(artifact(name)["bytecode_runtime"][2:])
        if not code.startswith(runtime):
            raise ValueError(f"Unexpected {name} bytecode")
        for getter, value in expected[name].items():
            if instance.functions[getter]().call(block_identifier=block) != value:
                raise ValueError(f"Unexpected {name} {getter}")
    return contracts["funding"], contracts["proceeds"]


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


def reconcile(client, c):
    final = client.eth.get_block("finalized")
    block = final["number"]
    if c.get("deployment_block") is None or c["deployment_block"] > block:
        raise ValueError("A finalized deployment_block is required")
    funding, proceeds = verify(client, c, block)
    read = lambda fn: fn.call(block_identifier=block)
    events = lambda event: logs(client, event, c["deployment_block"], block)
    purchases, sales = (
        events(funding.events.Purchased()),
        events(proceeds.events.Sold()),
    )
    captures, accruals = (
        events(proceeds.events.Snapshot()),
        events(proceeds.events.Accrued()),
    )
    rows = {
        int(x["args"]["week"]): {
            "week": int(x["args"]["week"]),
            "paid": int(x["args"]["usdc"]),
            "purchase_tx": Web3.to_hex(x["transactionHash"]),
        }
        for x in purchases
    }
    if len(rows) != len(purchases) or sum(r["paid"] for r in rows.values()) != read(
        funding.functions.spent()
    ):
        raise ValueError("Purchase ledger mismatch")
    snapshots = {int(x["args"]["week"]): x["args"] for x in captures}
    if len(snapshots) != len(captures):
        raise ValueError("Duplicate weekly snapshot")
    totals = dict(earned=0, sold=0, output=0, retained=0)
    warnings = []
    cursor = read(proceeds.functions.cursor())
    dust = read(proceeds.functions.DUST())
    for week, row in sorted(rows.items()):
        state = named(proceeds.functions.weeks(week), block)
        earned = sum(
            int(x["args"]["crv"]) for x in accruals if x["args"]["week"] == week
        )
        sold = [x["args"] for x in sales if x["args"]["week"] == week]
        sums = dict(
            earned=earned,
            sold=sum(x["crv"] for x in sold),
            output=sum(x["crvusd"] for x in sold),
            retained=sum(x["yearn"] for x in sold),
        )
        if (
            any(state[k] != v for k, v in sums.items())
            or state["sold"] > state["earned"]
        ):
            raise ValueError("Weekly ledger mismatch")
        if any(x["yearn"] + x["vyper"] != x["crvusd"] for x in sold):
            raise ValueError("Sale conservation mismatch")
        if state["ready"]:
            snap = snapshots[week]
            if (
                snap["last_vote"] >= week
                or weight(snap["slope"], snap["end"], week) != state["yearn"]
            ):
                raise ValueError("Vote calculation mismatch")
            total = read(
                contract(client, "controller").functions.points_weight(
                    ADDRESSES["gauge"], week
                )
            )[0]
            if (
                total != state["total"]
                or snap["yearn"] != state["yearn"]
                or snap["total"] != total
            ):
                raise ValueError("Gauge weight mismatch")
            if (
                state["output"]
                and state["retained"] != state["output"] * state["yearn"] // total
            ):
                raise ValueError("Proceeds split mismatch")
            if bytes(snap["evidence"]) != bytes(32):
                vote = historical_vote(client, week, final)
                if weight(vote["slope"], vote["end"], week) != state["yearn"]:
                    raise ValueError("Recovered vote differs from historical state")
        elif week <= final["timestamp"]:
            warnings.append(
                "Weekly accounting needs a checkpoint or historical recovery"
            )
        row.update(state)
        row["status"] = (
            "Scheduled"
            if week > final["timestamp"]
            else "Needs checkpoint"
            if not state["ready"]
            or (week + WEEK <= final["timestamp"] and cursor < week + WEEK)
            else "Earning"
            if week + WEEK > final["timestamp"]
            else "Selling"
            if state["earned"] - state["sold"] > dust
            else "Settled"
        )
        for key, value in sums.items():
            totals[key] += value
    for key, value in totals.items():
        if value != read(proceeds.functions["total_" + key]()):
            raise ValueError("Lifetime ledger mismatch")
    if read(proceeds.functions.paused()):
        warnings.append("Sales are paused")
    if not read(proceeds.functions.cursor()):
        warnings.append("Programme is not activated")
    report = {
        "as_of": final["timestamp"],
        "block": block,
        "block_hash": Web3.to_hex(final["hash"]),
        "funding": funding.address,
        "proceeds": proceeds.address,
        "budget": BUDGET,
        "spent": read(funding.functions.spent()),
        "funding_assets": read(funding.functions.assets()),
        "proceeds_balance": read(
            contract(client, "token", ADDRESSES["crvusd"]).functions.balanceOf(
                proceeds.address
            )
        ),
        "totals": totals,
        "weeks": list(sorted(rows.values(), key=lambda r: r["week"])),
        "withdrawn_crvusd": sum(
            x["args"]["amount"]
            for x in events(proceeds.events.Recovered())
            if x["args"]["token"] == ADDRESSES["crvusd"]
        ),
        "warnings": list(dict.fromkeys(warnings)),
    }
    if client.eth.get_block(block)["hash"] != final["hash"]:
        raise ValueError("Source block reorganized; rerun")

    # Preserve 18-decimal amounts exactly through JSON and JavaScript.
    def serialize(x):
        if isinstance(x, bool):
            return x
        if isinstance(x, int):
            return str(x)
        if isinstance(x, dict):
            return {k: serialize(v) for k, v in x.items()}
        if isinstance(x, list):
            return [serialize(v) for v in x]
        return x

    return serialize(report)
