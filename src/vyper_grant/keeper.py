"""One keeper action per pass; persist signed transactions before broadcasting."""

import fcntl
import json
import os
from pathlib import Path
from web3 import Web3
from web3.exceptions import TransactionNotFound
from .core import address, atomic_json, units
from .chain import verify


def pending(client, journal):
    if not journal.exists():
        return None
    tx = json.loads(journal.read_text())
    try:
        receipt = client.eth.get_transaction_receipt(tx["hash"])
    except TransactionNotFound:
        try:
            client.eth.get_transaction(tx["hash"])
        except TransactionNotFound:
            client.eth.send_raw_transaction(bytes.fromhex(tx["raw"]))
        return {"status": "pending", "tx": tx["hash"]}
    block = client.eth.get_block(receipt["blockNumber"])
    if (
        block["hash"] != receipt["blockHash"]
        or client.eth.block_number < receipt["blockNumber"] + 2
    ):
        return {"status": "confirming", "tx": tx["hash"]}
    journal.unlink()
    if receipt["status"] != 1:
        raise ValueError("Keeper transaction reverted: " + tx["hash"])
    return None


def run(client, c, send=False, directory="output/keeper"):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = root / "pending.json"
        if journal.exists():
            saved = json.loads(journal.read_text())
            if saved.get("sender") != c.get("keeper") or saved.get("escrow") != c.get(
                "proceeds"
            ):
                raise ValueError(
                    "Pending transaction belongs to another keeper configuration"
                )
            if Web3.to_hex(Web3.keccak(bytes.fromhex(saved["raw"]))) != saved["hash"]:
                raise ValueError("Pending transaction journal is corrupt")
            if not send:
                return {
                    "status": "pending",
                    "tx": json.loads(journal.read_text())["hash"],
                }
            waiting = pending(client, journal)
            if waiting:
                return waiting
        _, escrow = verify(client, c)
        sender = address(c.get("keeper"))
        f = escrow.functions
        if not f.keepers(sender).call():
            raise ValueError("Keeper is not authorized")
        if f.paused().call() or not f.cursor().call():
            return {"status": "paused or inactive"}
        now = client.eth.get_block("latest")["timestamp"]
        week, available = f.next_sale().call()
        if available >= units(c["min_trade_crv"]) or (
            available and now >= week + 604800
        ):
            if now < f.last_sale().call() + f.cooldown().call():
                return {"status": "cooldown"}
            quote = f.quote(available).call()
            minimum = quote * (10000 - int(c["slippage_bps"])) // 10000
            fn = f.sell(week, available, minimum, now + 300)
            action = {
                "status": "sale",
                "week": week,
                "crv": str(available),
                "min_crvusd": str(minimum),
            }
        else:
            # eth_call computes all accounting checks without changing the chain.
            booked = f.checkpoint().call({"from": sender})
            current = now // 604800 * 604800
            cursor, end = f.cursor().call(), f.END().call()
            snapshot_due = cursor < current and (current < end or cursor < end <= now)
            if booked < units(c["min_trade_crv"]) and not snapshot_due:
                return {"status": "below minimum"}
            fn, action = f.checkpoint(), {"status": "checkpoint", "crv": str(booked)}
        fn.call({"from": sender})
        if not send:
            return {
                **action,
                "to": escrow.address,
                "data": fn._encode_transaction_data(),
                "dry_run": True,
            }
        account = client.eth.account.from_key(os.environ["KEEPER_PRIVATE_KEY"])
        if account.address != sender:
            raise ValueError("Keeper key does not match configured address")
        if client.eth.gas_price > units(c["max_gas_gwei"], 9):
            return {"status": "gas limit"}
        nonce = client.eth.get_transaction_count(sender)
        if nonce != client.eth.get_transaction_count(sender, "pending"):
            return {"status": "untracked pending nonce"}
        tx = fn.build_transaction({"from": sender, "nonce": nonce, "chainId": 1})
        # Leave room for another checkpoint or a rollover before inclusion.
        tx["gas"] = tx["gas"] * 12 // 10 + 100_000
        if tx.get("maxFeePerGas", tx.get("gasPrice", 0)) > units(c["max_gas_gwei"], 9):
            return {"status": "gas limit"}
        signed = account.sign_transaction(tx)
        tx_hash = Web3.to_hex(signed.hash)
        atomic_json(
            journal,
            {
                "hash": tx_hash,
                "raw": bytes(signed.raw_transaction).hex(),
                "nonce": nonce,
                "sender": sender,
                "escrow": escrow.address,
            },
        )
        client.eth.send_raw_transaction(signed.raw_transaction)
        return {**action, "tx": tx_hash}
