"""A single signer submits at most one transaction per pass; chain state is authoritative."""

import fcntl
import os
from pathlib import Path
from .core import WEEK, address, contract, named, units


def run(client, c, send=False, directory="output/keeper", checkpoint_only=False):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sender = address(c.get("keeper"))
        nonce = client.eth.get_transaction_count(sender)
        if nonce != client.eth.get_transaction_count(sender, "pending"):
            return {"status": "pending nonce"}
        escrow = contract(client, "proceeds", c["proceeds"])
        f = escrow.functions
        head = client.eth.get_block("latest")
        now, block = head["timestamp"], head["number"]
        def read(fn):
            return fn.call(block_identifier=block)
        week = now // WEEK * WEEK
        current = named(f.weeks(week), block)
        start, end = read(f.START()), read(f.END())
        snapshot_due = start <= week < end and not current["ready"]
        closing = now % WEEK >= WEEK - 300 or end <= now < end + read(f.cooldown()) + 300
        paused = read(f.paused())
        can_work = read(f.canWork())
        authorized = read(f.keepers(sender))
        fn = f.checkpoint()
        action = {"status": "checkpoint"}
        if not checkpoint_only and can_work and authorized:
            # Simulate the gauge checkpoint, then quote that amount at the same block.
            amount = read(f.claimable())
            output = read(f.quote(amount))
            minimum = output * (10000 - c["slippage_bps"]) // 10000
            if minimum > 0 and (closing or amount >= units(c["min_trade_crv"])):
                # Allow ordinary claim growth while rejecting a stale oversized sale.
                max_crv = amount * 4 + 10**18
                fn = f.work(minimum, week, amount, max_crv)
                action = {
                    "status": "sale",
                    "crv": str(amount),
                    "max_crv": str(max_crv),
                    "week": week,
                    "min_usdc": str(minimum),
                }
            elif not snapshot_due:
                return {"status": "below minimum"}
        elif not (checkpoint_only or snapshot_due):
            if not authorized:
                raise ValueError("Keeper is not authorized")
            return {"status": "paused" if paused else "cooldown"}
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
        gas_cap = units(c["max_gas_gwei"], 9)
        if client.eth.gas_price > gas_cap:
            return {"status": "gas limit"}
        tx = fn.build_transaction({"from": sender, "nonce": nonce, "chainId": 1})
        tx["gas"] = tx["gas"] * 12 // 10 + 100_000
        if tx.get("maxFeePerGas", tx.get("gasPrice", 0)) > gas_cap:
            return {"status": "gas limit"}
        signed = account.sign_transaction(tx)
        tx_hash = client.eth.send_raw_transaction(signed.raw_transaction)
        return {**action, "tx": client.to_hex(tx_hash)}
