"""A single signer submits at most one transaction per pass; chain state is authoritative."""

import fcntl
import os
from pathlib import Path
from .core import WEEK, address, contract, named, units


def run(client, c, send=False, directory="output/keeper", sync_only=False):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sender = address(c.get("keeper"))
        nonce = client.eth.get_transaction_count(sender)
        if nonce != client.eth.get_transaction_count(sender, "pending"):
            return {"status": "pending nonce"}
        funding = contract(client, "funding", c["funding"])
        escrow = contract(client, "proceeds", c["proceeds"])
        f = escrow.functions
        head = client.eth.get_block("latest")
        now, block = head["timestamp"], head["number"]
        read = lambda fn: fn.call(block_identifier=block)
        week = now // WEEK * WEEK
        cursor, start, end = read(f.cursor()), read(f.START()), read(f.END())
        current = named(f.weeks(week), block)
        boundary_due = (
            (
                start <= week < end
                and read(funding.functions.paid(week)) > 0
                and not current["ready"]
            )
            or (
                start <= week + WEEK <= end
                and now % WEEK >= WEEK - 300
                and cursor < week + WEEK - 300
            )
            or (cursor < end <= now)
        )
        # eth_call uses the actual sync code, including pending earnings, without a transaction.
        available = read(f.sync())
        fn, action = f.sync(), {"status": "sync", "available_crv": str(available)}
        paused = read(f.paused())
        authorized = read(f.keepers(sender))
        amount = min(available, units(c["max_trade_crv"]))
        can_sell = amount > 0 and (amount >= units(c["min_trade_crv"]) or now >= end)
        if not sync_only and not paused and authorized and can_sell:
            minimum = read(f.quote(amount)) * (10000 - c["slippage_bps"]) // 10000
            if minimum == 0:
                return {"status": "below pool precision"}
            fn = f.sell(amount, minimum, now + 300)
            action = {"status": "sale", "crv": str(amount), "min_crvusd": str(minimum)}
        elif not (sync_only or boundary_due):
            if not authorized:
                raise ValueError("Keeper is not authorized")
            return {"status": "paused" if paused else "below minimum"}
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
