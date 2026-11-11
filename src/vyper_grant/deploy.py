"""Deploy both escrows using an encrypted Brownie account, then verify on Etherscan."""

import argparse
import getpass
import json
import os
import time
from pathlib import Path

import requests
from eth_account import Account
from web3 import Web3

from .chain import verify
from .cli import deployment
from .core import CONTRACTS, ROOT, address, artifact, atomic_json, load_config, rpc


def verification_input(name):
    """Compile the exact explorer input and require the committed deployment artifact."""
    import vyper
    from vyper.cli.vyper_json import compile_json

    if vyper.__version__ != "0.4.3":
        raise ValueError("Vyper 0.4.3 is required; run with --extra dev")
    path = f"contracts/{CONTRACTS[name]}.vy"
    source = {
        "language": "Vyper",
        "sources": {path: {"content": (ROOT / path).read_text()}},
        "settings": {
            "evmVersion": "prague",
            "outputSelection": {"*": ["abi", "evm.bytecode", "evm.deployedBytecode"]},
        },
    }
    result = compile_json(source)["contracts"][path][CONTRACTS[name]]
    compiled = artifact(name)
    if (
        result["abi"] != compiled["abi"]
        or result["evm"]["bytecode"]["object"] != compiled["bytecode"]
        or result["evm"]["deployedBytecode"]["object"] != compiled["bytecode_runtime"]
    ):
        raise ValueError("Source/artifact mismatch: rebuild and review before deploying")
    return source


def etherscan(action, **fields):
    response = requests.post(
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": 1, "module": "contract", "action": action,
            "apikey": os.environ["ETHERSCAN_KEY"],
        },
        data=fields,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def publish(name, deployed, source):
    for _ in range(12):
        result = etherscan(
            "verifysourcecode",
            codeformat="vyper-json",
            sourceCode=json.dumps(source),
            contractaddress=deployed,
            contractname=f"contracts/{CONTRACTS[name]}.vy:{CONTRACTS[name]}",
            compilerversion="vyper:0.4.3",
            optimizationUsed="1",
            constructorArguments="",
        )
        if "unable to locate contractcode" not in str(result.get("result", "")).lower():
            break
        time.sleep(5)
    if "already verified" in str(result.get("result", "")).lower():
        return
    if result.get("status") != "1":
        raise ValueError(f"Etherscan rejected {name}: {result.get('result')}")
    guid = result["result"]
    for _ in range(24):
        time.sleep(5)
        result = etherscan("checkverifystatus", guid=guid)
        message = str(result.get("result", ""))
        if result.get("status") == "1" or "already verified" in message.lower():
            return
        if "pending" not in message.lower() and "queue" not in message.lower():
            raise ValueError(f"Etherscan verification failed for {name}: {message}")
    raise ValueError(f"Etherscan still processing {name}; retry with --verify-only")


def deploy_contract(client, name, account, record, journal):
    """Record the signed transaction hash before sending; never silently redeploy."""
    if "tx" not in record:
        nonce = client.eth.get_transaction_count(account.address)
        if client.eth.get_transaction_count(account.address, "pending") != nonce:
            raise ValueError("Account has a pending transaction; resolve it before deploying")
        tx = deployment(client, {}, name, account.address)["transaction"]
        tx.update(nonce=nonce, gas=tx["gas"] * 12 // 10)
        signed = account.sign_transaction(tx)
        record["tx"] = Web3.to_hex(signed.hash)
        atomic_json(journal[0], journal[1])
        print(f"{CONTRACTS[name]} transaction: {record['tx']}", flush=True)
        client.eth.send_raw_transaction(signed.raw_transaction)
    receipt = client.eth.wait_for_transaction_receipt(record["tx"], timeout=180)
    if receipt.status != 1 or not receipt.contractAddress:
        raise ValueError(f"{name} deployment reverted; inspect {record['tx']}")
    deployed = receipt.contractAddress
    verify(client, {name: deployed}, names=(name,))
    record.update(address=deployed, block=receipt.blockNumber)
    atomic_json(journal[0], journal[1])
    return deployed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default="wavey3", help="Brownie account name")
    parser.add_argument("--config", type=Path, default=Path("config/live.json"))
    parser.add_argument("--journal", type=Path, default=Path("build/deployment.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--broadcast", action="store_true", help="Unlock, deploy and verify")
    mode.add_argument("--verify-only", action="store_true", help="Verify saved deployments")
    args = parser.parse_args()
    try:
        execute(args)
    except Exception as exc:
        # Provider/explorer exceptions can contain private RPC URLs or API keys.
        message = str(exc)
        for key in ["ETHEREUM_RPC_URL", "MAINNET_RPC_URL", "ETHERSCAN_KEY"]:
            if os.environ.get(key):
                message = message.replace(os.environ[key], "[redacted]")
        raise SystemExit(
            message + "\nAny recorded deployment transaction is retained in the journal."
        ) from None


def execute(args):
    client = rpc()
    sources = {name: verification_input(name) for name in CONTRACTS}
    c = load_config(args.config if args.config.exists() else ROOT / "config/example.json")
    state = json.loads(args.journal.read_text()) if args.journal.exists() else {}
    fingerprint = Web3.to_hex(Web3.keccak(text=json.dumps(sources, sort_keys=True)))
    if state and state.get("build") != fingerprint:
        raise ValueError("Deployment journal belongs to another build; restore that source to resume")
    state["build"] = fingerprint
    for name in CONTRACTS:
        record = state.setdefault(name, {})
        if c.get(name) and c[name] != record.get("address"):
            raise ValueError(f"Config already contains an unrecorded {name} deployment; refusing to overwrite")

    if args.verify_only:
        if not all(state[n].get("address") for n in CONTRACTS):
            raise ValueError("Both recorded deployment addresses are required")
    else:
        if Path(args.account).name != args.account:
            raise ValueError("Use a Brownie account name, not a path")
        keystore_path = Path.home() / ".brownie/accounts" / (args.account + ".json")
        keystore = json.loads(keystore_path.read_text())
        sender = address("0x" + keystore["address"].removeprefix("0x"))
        if state.get("deployer", sender) != sender:
            raise ValueError("Deployment journal belongs to another account")
        state["deployer"] = sender
        print(f"Ethereum mainnet · {args.account}: {sender}")
        cost = 0
        for name in CONTRACTS:
            if state[name].get("tx"):
                print(f"{CONTRACTS[name]}: resuming {state[name]['tx']}")
                continue
            tx = deployment(client, c, name, sender)["transaction"]
            gas = tx["gas"] * 12 // 10
            cost += gas * tx.get("maxFeePerGas", tx.get("gasPrice", 0))
            print(f"{CONTRACTS[name]}: {gas:,} gas limit · no constructor arguments")
        print(f"Maximum estimated deployment gas cost: {Web3.from_wei(cost, 'ether')} ETH")
        if client.eth.get_balance(sender) < cost:
            raise ValueError("Insufficient ETH for estimated deployment gas")
        if not args.broadcast:
            print("Dry run only. Add --broadcast to deploy and verify.")
            return
        if not os.environ.get("ETHERSCAN_KEY"):
            raise ValueError("ETHERSCAN_KEY is required before deployment")
        needs_signer = any(not state[n].get("tx") for n in CONTRACTS)
        account = None
        if needs_signer:
            account = Account.from_key(Account.decrypt(
                keystore, getpass.getpass(f"Password for {args.account}: ")
            ))
            if account.address != sender:
                raise ValueError("Keystore address does not match its signing key")
        for name in CONTRACTS:
            c[name] = deploy_contract(
                client, name, account, state[name], (args.journal, state)
            )
            c["deployment_block"] = min(
                state[n]["block"] for n in CONTRACTS if state[n].get("block")
            )
            atomic_json(args.config, c)
            print(f"{CONTRACTS[name]}: https://etherscan.io/address/{c[name]}", flush=True)

    if not os.environ.get("ETHERSCAN_KEY"):
        raise ValueError("ETHERSCAN_KEY is required")
    for name in CONTRACTS:
        deployed = state[name]["address"]
        verify(client, {name: deployed}, names=(name,))
        print(f"Verifying {CONTRACTS[name]}…", flush=True)
        publish(name, deployed, sources[name])
        print(f"Verified: https://etherscan.io/address/{deployed}#code", flush=True)
    print(f"Saved {args.config}. Next: configure keeper and prepare the two Safe setup batches.")


if __name__ == "__main__":
    main()
