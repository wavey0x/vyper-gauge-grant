from __future__ import annotations
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from fractions import Fraction
from functools import lru_cache
from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "contracts/VyperFundingEscrow.vy").exists():
    ROOT = Path(__file__).resolve().parent / "assets"
WEEK = 604800
UNIT = 10**6
TARGET = 100_000 * UNIT
ROUND_CAP = 25_000 * UNIT
ZERO = "0x" + "0" * 40
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
MAX_PRICE_AGE = 36 * 3600
ADDRESSES = {
    "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "gauge": "0x3b8EFAa0A1Fca3d31144002f6D9c7412B967E0b4",
    "receiver": "0x70CCBE10F980d80b7eBaab7D2E3A73e87D67B775",
    "controller": "0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB",
    "minter": "0xd061D61a4d941c39E5453435B6345Dc261C2fcE0",
    "feed": "0xCd627aA160A6fA45Eb793D19Ef54f5062F20f33f",
    "votium": "0x63942E31E98f1833A234077f47880A66136a2D1e",
    "remote": "0x177198aDb759a9715bC7259BE1b7bE535BeD7542",
    "market": "0x8c2c5A295450DDFf4CB360cA73FCCC12243D14D9",
    "convex": "0x64D9B5AC386B70af9EDCD20A58cE9262D2EAC278",
    "convex_voter": "0x989AEb4d175e16225E39E87d0D97A3360524AD80",
}


def address(value):
    if not value or not Web3.is_address(value) or int(value, 16) == 0:
        raise ValueError("A nonzero address is required")
    return Web3.to_checksum_address(value)


def load_config(path):
    c = json.loads(Path(path).read_text())
    for key in [
        "usdc",
        "gauge",
        "receiver",
        "controller",
        "minter",
        "feed",
        "votium",
        "remote",
        "market",
    ]:
        if (
            c.get("addresses", {}).get(key, ADDRESSES[key]).lower()
            != ADDRESSES[key].lower()
        ):
            raise ValueError("Production dependency cannot be overridden: " + key)
    c["addresses"] = {**ADDRESSES, **c.get("addresses", {})}
    c["addresses"] = {k: address(v) for k, v in c["addresses"].items()}
    for k in ["treasury", "management", "campaign_manager", "escrow"]:
        if c.get(k):
            c[k] = address(c[k])
    if not c.get("campaign_manager") and c.get("management"):
        c["campaign_manager"] = c["management"]
    return c


def rpc(env_name, chain_id):
    url = os.environ.get(env_name)
    if not url:
        raise ValueError(f"{env_name} is not set")
    client = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    if client.eth.chain_id != chain_id:
        raise ValueError(f"{env_name} has the wrong chain ID")
    return client


@lru_cache
def abi(name):
    return json.loads((ROOT / "abi" / f"{name}.json").read_text())


@lru_cache
def artifact():
    from vyper import compile_code

    return compile_code(
        (ROOT / "contracts/VyperFundingEscrow.vy").read_text(),
        output_formats=["abi", "bytecode"],
    )


def contract(client, name, addr=None):
    return client.eth.contract(
        address=address(addr or ADDRESSES[name]),
        abi=artifact()["abi"] if name == "escrow" else abi(name),
    )


def call(client, addr, name, inputs=(), outputs=("uint256",), args=(), block="latest"):
    a = [
        {
            "type": "function",
            "name": name,
            "stateMutability": "view",
            "inputs": [{"type": t, "name": ""} for t in inputs],
            "outputs": [{"type": t, "name": ""} for t in outputs],
        }
    ]
    return (
        client.eth.contract(address=address(addr), abi=a)
        .functions[name](*args)
        .call(block_identifier=block)
    )


def check_price(answer, timestamp):
    """Reject a non-positive, future or stale Chainlink latestRoundData answer."""
    _, price, _, updated, _ = answer
    if (
        price <= 0
        or not updated
        or updated > timestamp
        or timestamp - updated > MAX_PRICE_AGE
    ):
        raise ValueError("Invalid or stale CRV/USD oracle price")
    return answer


def multicall(client, calls, block):
    """Run (contract function, ...) calls in ONE eth_call so simulated writes persist
    for later reads; separate eth_calls discard state changes. Returns raw results."""
    targets = [
        (f.address, bytes.fromhex(f._encode_transaction_data()[2:])) for f in calls
    ]
    _, results = (
        client.eth.contract(address=MULTICALL, abi=abi("multicall"))
        .functions.aggregate(targets)
        .call(block_identifier=block)
    )
    return results


class Pending(ValueError):
    """Evidence is not available yet; the week stays pending and planning holds."""


def named(fn, block):
    """Call a view function and key its outputs (or its single struct) by ABI name."""
    outputs = fn.abi["outputs"]
    result = fn.call(block_identifier=block)
    if len(outputs) == 1 and outputs[0]["type"] == "tuple":
        outputs, result = outputs[0]["components"], result
    elif len(outputs) == 1:
        result = [result]
    return {o["name"]: v for o, v in zip(outputs, result)}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def usdc(value):
    n = Decimal(value) * UNIT
    if n < 0 or n != n.to_integral_value():
        raise ValueError("USDC amounts require at most six decimals")
    return int(n)


def sorted_addresses(values):
    out = [address(v) for v in values]
    if len(set(out)) != len(out) or len(out) > 50:
        raise ValueError("Exclusions must be unique and at most 50")
    return sorted(out, key=lambda x: int(x, 16))


def credit(total_weight, vm_weight, convex_weight, convex_excluded, convex_total):
    """Return one exact ratio; vlCVX is converted through actual Convex gauge weight."""
    if min(total_weight, vm_weight, convex_weight, convex_excluded, convex_total) < 0:
        raise ValueError("Negative voting weight")
    if convex_excluded > convex_total:
        raise ValueError("Excluded vlCVX exceeds total")
    if convex_weight and not convex_total:
        raise ValueError("Missing Convex total")
    eligible = Fraction(vm_weight) + (
        Fraction(convex_weight) * (convex_total - convex_excluded) / convex_total
        if convex_total
        else 0
    )
    if eligible > total_weight:
        raise ValueError("Eligible votes overlap or exceed gauge total")
    return eligible / total_weight if total_weight else Fraction(0)


def value_for_week(start_accrued, end_accrued, minted, ratio, price):
    if end_accrued < start_accrued or price <= 0:
        raise ValueError("Invalid accrual or price")
    gross = end_accrued - start_accrued
    received = max(0, min(end_accrued, minted) - start_accrued)
    return {
        "gross_crv": gross,
        "credited_crv": int(gross * ratio),
        "received_credited_crv": int(received * ratio),
        "accrued_value_usdc": int(gross * ratio * price / 10**20),
        "received_value_usdc": int(received * ratio * price / 10**20),
    }
