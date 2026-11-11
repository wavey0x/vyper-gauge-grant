"""A single signer submits at most one transaction per pass; chain state is authoritative."""

import fcntl
import os
from pathlib import Path
from .core import WEEK, address, contract, named, units


def quote_work(escrow, c, block):
    """Quote only this batch's swap; simulate the share checkpoint when retaining CRV."""
    f = escrow.functions
    amount = min(f.claimable().call(block_identifier=block), units(c.get("max_crv", "15000")))
    sell_vyper = c.get("sell_vyper", True)
    swap = amount if sell_vyper else amount * f.checkpoint().call(block_identifier=block) // 10**18
    return {
        "crv": amount,
        "swap_crv": swap,
        "vyper_crv": amount - swap,
        "crvusd": f.quote(swap).call(block_identifier=block),
        "sell_vyper": sell_vyper,
    }


def run(client, c, send=False, directory="output/keeper", checkpoint_only=False, funding_only=False):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sender = address(c.get("keeper"))
        nonce = client.eth.get_transaction_count(sender)
        if nonce != client.eth.get_transaction_count(sender, "pending"):
            return {"status": "pending nonce"}
        if funding_only:
            escrow = contract(client, "funding", c["funding"])
            f = escrow.functions
            block = client.eth.block_number
            if not f.canWork().call(block_identifier=block):
                return {"status": "not due"}
            fn = f.work()
            action = {
                "status": "funding payment",
                "usdc": str(f.payments(0).call(block_identifier=block)),
                "week": f.next_week().call(block_identifier=block),
            }
        else:
            escrow = contract(client, "proceeds", c["proceeds"])
            f = escrow.functions
            head = client.eth.get_block("latest")
            now, block = head["timestamp"], head["number"]
            def read(fn):
                return fn.call(block_identifier=block)
            week = now // WEEK * WEEK
            current = named(f.weeks(week), block)
            snapshot_due = not current["ready"]
            closing = now % WEEK >= WEEK - 300
            paused = read(f.paused())
            can_work = read(f.canWork())
            authorized = read(f.keepers(sender))
            fn = f.checkpoint()
            action = {"status": "checkpoint"}
            if not checkpoint_only and can_work and authorized:
                quote = quote_work(escrow, c, block)
                amount, output = quote["crv"], quote["crvusd"]
                minimum = output * (10000 - c["slippage_bps"]) // 10000
                if amount > 0 and (quote["swap_crv"] == 0 or minimum > 0) and (closing or amount >= units(c["min_trade_crv"])):
                    fn = f.work(minimum, amount, quote["sell_vyper"])
                    action = {
                        "status": "sale" if quote["swap_crv"] else "retain",
                        "crv": str(amount),
                        "swap_crv": str(quote["swap_crv"]),
                        "vyper_crv": str(quote["vyper_crv"]),
                        "sell_vyper": quote["sell_vyper"],
                        "max_crv": str(amount),
                        "week": week,
                        "min_crvusd": str(minimum),
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
