"""A single signer submits at most one transaction per pass; chain state is authoritative."""

import fcntl
import os
import time
from pathlib import Path
from web3.exceptions import ContractLogicError
from .chain import verify
from .core import ADDRESSES, WEEK, address, contract, named, units

TICK = 300  # cron period; ops/run.sh records each launch time in KEEPER_TICK
TICK_EARLY, TICK_LATE = 30, 60  # launch jitter still treated as the scheduled tick
CUTOFF = 36  # no proceeds transaction is prepared or signed this close to a boundary
MAX_HEAD_AGE = 36  # a closing sale needs a head at most this old
MAX_CLOCK_SKEW = 5  # a head further than this in the future means a bad clock
HOLD_MARGIN = 330  # the 23:55 head (W-301) + one slot (12 s) + margin
FUNDING_QUIET = 600  # funding stays off the shared nonce from 23:50 until the new week
BOUNDARY_GAS = 1_000_000  # headroom for the controller's weekly update (~892k), on every proceeds tx
LOCK_WAIT, LOCK_POLL = 60, 0.5
DUST_CRV = 10**12  # TriCRV quotes revert or round to zero below about this amount
FAILURES = {"simulation failed", "checkpoint failed"}  # the CLI exits 3 for these
# Revert strings from the escrows; anything else is reported as a generic revert.
KNOWN_REVERTS = {
    "already captured", "cooldown", "forward", "keeper or paused", "min out",
    "missing vote history", "not due", "nothing to sell", "pull", "return", "week", "weight",
}


def wall_clock():
    return time.time()


def scheduled_tick(capture=None):
    """Return the five-minute tick this run was launched for, or None when unscheduled."""
    if capture is None:
        value = os.environ.get("KEEPER_TICK")
        if not value:
            return None
        capture = int(value)
    tick = (capture + TICK // 2) // TICK * TICK
    return tick if tick - TICK_EARLY <= capture <= tick + TICK_LATE else None


def timing(client, clock):
    head = client.eth.get_block("latest")
    now = clock()
    return {
        "block": head["number"],
        "ts": head["timestamp"],
        "clock": now,
        "head_left": WEEK - head["timestamp"] % WEEK,
        "clock_left": WEEK - now % WEEK,
        "head_age": now - head["timestamp"],
    }


def resample(t, clock):
    """Take a new clock reading against the same head, after any RPC calls made since."""
    now = clock()
    return {**t, "clock": now, "clock_left": WEEK - now % WEEK, "head_age": now - t["ts"]}


def proceeds_window(t, tick, cooldown):
    """Return (skip status or None, closing, may_sell) for one proceeds pass."""
    if t["head_age"] < -MAX_CLOCK_SKEW:
        return "clock skew", False, False
    week = t["ts"] // WEEK
    if int(t["clock"]) // WEEK != week or (tick is not None and tick // WEEK != week):
        return "boundary", False, False
    if tick is not None and tick % WEEK == 0:
        return "boundary", False, False
    if t["clock_left"] <= CUTOFF or t["head_left"] <= CUTOFF:
        return "boundary", False, False
    closing = (
        tick is not None
        and (tick // WEEK + 1) * WEEK - tick == TICK
        and t["head_age"] <= MAX_HEAD_AGE
    )
    return None, closing, closing or t["clock_left"] > cooldown + HOLD_MARGIN


def checkpoint_window(t):
    """The explicit checkpoint command skips the sale schedule but needs a current-week head."""
    if t["head_age"] < -MAX_CLOCK_SKEW:
        return "clock skew"
    if int(t["clock"]) // WEEK != t["ts"] // WEEK:
        return "boundary"
    return None


def funding_window(t):
    if t["head_age"] < -MAX_CLOCK_SKEW:
        return "clock skew"
    if (
        int(t["clock"]) // WEEK != t["ts"] // WEEK
        or t["clock_left"] <= FUNDING_QUIET
        or t["head_left"] <= FUNDING_QUIET
    ):
        return "closing window"
    return None


def gas_limit(estimate, proceeds):
    gas = estimate * 12 // 10 + 100_000
    return gas + BOUNDARY_GAS if proceeds else gas


def reason(error):
    """Report only known contract revert strings; never echo other provider text."""
    text = str(getattr(error, "message", None) or error)
    prefix = "execution reverted: "
    known = text.startswith(prefix) and text[len(prefix):] in KNOWN_REVERTS
    return text if known else "contract revert"


def acquire(lock, wait=None):
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL)


def quote_work(escrow, c, block):
    """Size one batch from allowance, mint permission and cap at one block, then quote its swap."""
    f, client = escrow.functions, escrow.w3
    receiver = ADDRESSES["receiver"]
    crv = contract(client, "token", ADDRESSES["crv"]).functions
    allowance = crv.allowance(receiver, escrow.address).call(block_identifier=block)
    mint_enabled = (
        contract(client, "minter")
        .functions.allowed_to_mint_for(escrow.address, receiver)
        .call(block_identifier=block)
    )
    # claimable() counts pending rewards even when mint_for would mint nothing.
    available = (
        f.claimable().call(block_identifier=block)
        if mint_enabled
        else crv.balanceOf(receiver).call(block_identifier=block)
    )
    amount = min(available, units(c.get("max_crv", "15000")), allowance)
    sell_vyper = c.get("sell_vyper", True)
    swap = amount if sell_vyper else amount * f.checkpoint().call(block_identifier=block) // 10**18
    usdc = 0 if 0 < swap < DUST_CRV else f.quote(swap).call(block_identifier=block)
    return {
        "crv": amount,
        "swap_crv": swap,
        "vyper_crv": amount - swap,
        "usdc": usdc,
        "sell_vyper": sell_vyper,
        # A positive swap that can't carry a positive minimum is never sent.
        "dust": swap > 0 and usdc * (10000 - c["slippage_bps"]) // 10000 == 0,
        "allowance": allowance,
        "mint_enabled": mint_enabled,
    }


def run(
    client,
    c,
    send=False,
    directory="output/keeper",
    checkpoint_only=False,
    funding_only=False,
    tick=None,
    clock=None,
):
    tick = scheduled_tick() if tick is None else tick
    clock = clock or wall_clock
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        if not acquire(lock):
            return {"status": "busy"}
        sender = address(c.get("keeper"))
        nonce = client.eth.get_transaction_count(sender)
        if nonce != client.eth.get_transaction_count(sender, "pending"):
            return {"status": "pending nonce"}
        proceeds = not funding_only
        sale = False
        if funding_only:
            (escrow,) = verify(client, c, names=("funding",))
            f = escrow.functions
            t = timing(client, clock)
            skip = funding_window(t)
            if skip:
                return {"status": skip}
            block = t["block"]
            if not f.canWork().call(block_identifier=block):
                return {"status": "not due"}
            fn = f.work()
            action = {
                "status": "funding payment",
                "usdc": str(f.payments(0).call(block_identifier=block)),
                "week": f.next_week().call(block_identifier=block),
            }
        else:
            (escrow,) = verify(client, c, names=("proceeds",))
            f = escrow.functions
            t = timing(client, clock)
            block = t["block"]

            def read(fn):
                return fn.call(block_identifier=block)

            week = t["ts"] // WEEK * WEEK
            snapshot_due = not named(f.weeks(week), block)["ready"]
            cooldown = read(f.cooldown())
            if checkpoint_only:
                skip = checkpoint_window(t)
            else:
                skip, closing, may_sell = proceeds_window(t, tick, cooldown)
            if skip:
                return {"status": skip}
            guard = None
            if c.get("guard"):
                # verify checks the guard's escrow and that its BOT is this signer.
                (guard,) = verify(client, c, names=("guard",))
                if not c.get("sell_vyper", True):
                    raise ValueError("The guard only sells; set sell_vyper to true")
            paused = read(f.paused())
            can_work = read(f.canWork())
            authorized = read(f.keepers(guard.address if guard else sender))
            fn = f.checkpoint()
            action = {"status": "checkpoint"}
            if checkpoint_only:
                pass
            elif not (can_work and authorized):
                if not snapshot_due:
                    if not authorized:
                        raise ValueError("Keeper is not authorized")
                    return {"status": "paused" if paused else "cooldown"}
            elif not may_sell:
                if not snapshot_due:
                    return {"status": "hold"}
                action["sale"] = "hold"
            else:
                try:
                    q = quote_work(escrow, c, block)
                except ContractLogicError as error:
                    failure = {"step": "quote", "reason": reason(error)}
                    return fallback(client, c, escrow, sender, nonce, send, tick, clock, failure, week)
                amount, swap = q["crv"], q["swap_crv"]
                minimum = q["usdc"] * (10000 - c["slippage_bps"]) // 10000
                state = {"allowance": str(q["allowance"]), "mint_enabled": q["mint_enabled"]}
                if q["allowance"] == 0:
                    skipped = "allowance exhausted"
                elif amount == 0:
                    skipped = "below minimum"
                elif q["dust"]:
                    skipped = "dust"
                elif not closing and amount < units(c["min_trade_crv"]):
                    skipped = "below minimum"
                else:
                    skipped = None
                if skipped and not snapshot_due:
                    return {"status": skipped, **state}
                if skipped:
                    action.update(sale=skipped, **state)
                else:
                    fn = (
                        guard.functions.work(minimum, amount)
                        if guard
                        else f.work(minimum, amount, q["sell_vyper"])
                    )
                    sale = True
                    action = {
                        "status": "sale" if swap else "retain",
                        "crv": str(amount),
                        "swap_crv": str(swap),
                        "vyper_crv": str(q["vyper_crv"]),
                        "sell_vyper": q["sell_vyper"],
                        "max_crv": str(amount),
                        "week": week,
                        "min_usdc": str(minimum),
                        "closing": closing,
                        **state,
                    }
        for step in ["simulate", "estimate"]:
            try:
                if step == "simulate":
                    fn.call({"from": sender})
                    if not send:
                        return {
                            **action,
                            "to": fn.address,
                            "data": fn._encode_transaction_data(),
                            "dry_run": True,
                        }
                else:
                    signed = sign(client, c, fn, sender, nonce, proceeds)
            except ContractLogicError as error:
                failure = {"step": step, "reason": reason(error)}
                if sale:
                    return fallback(client, c, escrow, sender, nonce, send, tick, clock, failure, week)
                if proceeds:
                    return {"status": "checkpoint failed", **failure}
                raise
        if isinstance(signed, dict):
            return signed
        fresh = timing(client, clock)
        if proceeds and fresh["ts"] // WEEK != t["ts"] // WEEK:
            # Never send a proceeds transaction prepared in an earlier week.
            return {"status": "boundary"}
        if funding_only or checkpoint_only:
            skip = funding_window(fresh) if funding_only else checkpoint_window(fresh)
            if skip:
                return {"status": skip}
        else:
            # Estimation can take long enough for the week, the safe window or the cooldown to change.
            cooldown = f.cooldown().call(block_identifier=fresh["block"])
            skip, _, may_sell = proceeds_window(resample(fresh, clock), tick, cooldown)
            if skip:
                return {"status": skip}
            if sale and not may_sell:
                return {"status": "hold"}
        tx_hash = client.eth.send_raw_transaction(signed.raw_transaction)
        return {**action, "tx": client.to_hex(tx_hash)}


def sign(client, c, fn, sender, nonce, proceeds):
    """Build, cap and sign one transaction, or return a status when it must not be sent."""
    account = client.eth.account.from_key(os.environ["KEEPER_PRIVATE_KEY"])
    if account.address != sender:
        raise ValueError("Keeper key does not match configured address")
    gas_cap = units(c["max_gas_gwei"], 9)
    if client.eth.gas_price > gas_cap:
        return {"status": "gas limit"}
    tx = fn.build_transaction({"from": sender, "nonce": nonce, "chainId": 1})
    tx["gas"] = gas_limit(tx["gas"], proceeds)
    price = tx.get("maxFeePerGas", tx.get("gasPrice", 0))
    if price > gas_cap:
        return {"status": "gas limit"}
    if client.eth.get_balance(sender) < tx["gas"] * price:
        return {"status": "low balance"}
    return account.sign_transaction(tx)


def fallback(client, c, escrow, sender, nonce, send, tick, clock, failure, week):
    """After a sale fails to prepare, record its week's due snapshot as a separately checked action."""
    f = escrow.functions
    status = {"status": "simulation failed", **failure}
    t = timing(client, clock)
    block = t["block"]
    skip, _, _ = proceeds_window(t, tick, f.cooldown().call(block_identifier=block))
    if skip or t["ts"] // WEEK * WEEK != week or named(f.weeks(week), block)["ready"]:
        return status
    fn = f.checkpoint()
    try:
        fn.call({"from": sender})
        if not send:
            return {
                **status,
                "fallback": "checkpoint",
                "to": escrow.address,
                "data": fn._encode_transaction_data(),
                "dry_run": True,
            }
        signed = sign(client, c, fn, sender, nonce, True)
    except ContractLogicError as error:
        return {**status, "status": "checkpoint failed", "fallback_reason": reason(error)}
    if isinstance(signed, dict):
        return {**status, "fallback": signed["status"]}
    fresh = timing(client, clock)
    if proceeds_window(fresh, tick, 0)[0] or fresh["ts"] // WEEK * WEEK != week:
        return status
    tx_hash = client.eth.send_raw_transaction(signed.raw_transaction)
    return {**status, "fallback": "checkpoint", "tx": client.to_hex(tx_hash)}
