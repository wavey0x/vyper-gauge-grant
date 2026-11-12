"""Fixed Ethereum dependencies, precise amounts, and reproducible artifacts."""

from functools import lru_cache
from decimal import Decimal
from pathlib import Path
import json
import os
import tempfile
from web3 import Web3

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "contracts/FundingEscrow.vy").exists():
    ROOT = Path(__file__).resolve().parent / "assets"
WEEK = 604800
ADDRESSES = {
    "manager": "0x4444AAAACDBa5580282365e25b16309Bd770ce4a",
    "governance": "0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52",
    "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "vault": "0xBe53A109B494E5c9f97b9Cd39Fe969BE68BF6204",
    "crv": "0xD533a949740bb3306d119CC777fa900bA034cd52",
    "crvusd": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
    "gauge": "0x3b8EFAa0A1Fca3d31144002f6D9c7412B967E0b4",
    "receiver": "0x70CCBE10F980d80b7eBaab7D2E3A73e87D67B775",
    "voter": "0xF147b8125d2ef93FB6965Db97D6746952a133934",
    "controller": "0x2F50D538606Fa9EDD2B11E2446BEb18C9D5846bB",
    "minter": "0xd061D61a4d941c39E5453435B6345Dc261C2fcE0",
    "pool": "0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14",
    "stable_pool": "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E",
}
CONTRACTS = {"funding": "FundingEscrow", "proceeds": "ProceedsEscrow"}


def address(value):
    if not value or not Web3.is_address(value) or int(value, 16) == 0:
        raise ValueError("A nonzero address is required")
    return Web3.to_checksum_address(value)


def units(value, decimals=18):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Amount must be finite")
    numerator, denominator = number.as_integer_ratio()
    numerator *= 10**decimals
    if numerator < 0 or numerator % denominator or numerator // denominator >= 2**256:
        raise ValueError(f"Amount must be nonnegative with at most {decimals} decimals")
    return numerator // denominator


def load_config(path):
    c = json.loads(Path(path).read_text())
    allowed = {
        "funding",
        "proceeds",
        "deployment_block",
        "keeper",
        "min_trade_crv",
        "max_crv",
        "sell_vyper",
        "slippage_bps",
        "max_gas_gwei",
    }
    if set(c) - allowed:
        raise ValueError(
            "Unknown or obsolete configuration fields: "
            + ", ".join(sorted(set(c) - allowed))
        )
    for key in ["funding", "proceeds", "keeper"]:
        if c.get(key):
            c[key] = address(c[key])
    for key in [
        "deployment_block",
        "slippage_bps",
    ]:
        if c.get(key) is not None and type(c[key]) is not int:
            raise ValueError(key + " must be an integer")
    if not 0 <= c["slippage_bps"] < 10000:
        raise ValueError("Slippage must be between 0 and 9999 bps")
    if units(c["min_trade_crv"]) == 0:
        raise ValueError("Minimum trade must be positive")
    if units(c.get("max_crv", "15000")) < units(c["min_trade_crv"]):
        raise ValueError("Maximum CRV must be at least the minimum trade")
    if type(c.get("sell_vyper", True)) is not bool:
        raise ValueError("sell_vyper must be a boolean")
    return c


@lru_cache
def artifact(name):
    return json.loads((ROOT / "abi/contracts.json").read_text())[name]


def build():
    """Compilation belongs to the build step, never to a keeper/report pass."""
    from vyper import compile_code

    result = {
        name: compile_code(
            (ROOT / "contracts" / (source + ".vy")).read_text(),
            output_formats=["abi", "bytecode", "bytecode_runtime"],
        )
        for name, source in CONTRACTS.items()
    }
    atomic_json(ROOT / "abi/contracts.json", result)
    artifact.cache_clear()
    abi.cache_clear()
    return result


@lru_cache
def abi(name):
    if name in CONTRACTS:
        return artifact(name)["abi"]
    return json.loads((ROOT / "abi/ethereum.json").read_text())[name]


def contract(client, name, addr=None):
    return client.eth.contract(address=address(addr or ADDRESSES[name]), abi=abi(name))


def rpc():
    url = os.environ.get("ETHEREUM_RPC_URL") or os.environ.get("MAINNET_RPC_URL")
    if not url:
        raise ValueError("ETHEREUM_RPC_URL is not set")
    client = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    if client.eth.chain_id != 1:
        raise ValueError("Ethereum mainnet RPC required")
    return client


def named(fn, block="latest"):
    outputs, result = fn.abi["outputs"], fn.call(block_identifier=block)
    if len(outputs) == 1 and outputs[0]["type"] == "tuple":
        outputs = outputs[0]["components"]
    elif len(outputs) == 1:
        result = [result]
    return {o["name"]: v for o, v in zip(outputs, result)}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
