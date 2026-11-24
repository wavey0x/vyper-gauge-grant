"""Scan both escrows together and keep a disposable checkpoint of their raw logs."""

import hashlib
import json
import sys

from hexbytes import HexBytes
from web3 import Web3

from .core import atomic_json

CHUNK_SIZE = 2000
EVENTS = {
    "funding": ("Drawn", "RoundFunded", "Cancelled", "Recovered", "RecipientSet"),
    "proceeds": ("Sold", "Snapshot", "Withdrawn"),
}


def _digest(history):
    return hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()


def save_checkpoint(path, history):
    atomic_json(path, {**history, "checksum": _digest(history)})


def _load_checkpoint(path, source):
    try:
        saved = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(saved, dict) or saved.get("source") != source:
        return None
    checksum = saved.pop("checksum", None)
    return saved if checksum == _digest(saved) else None


def scan_history(client, config, head, checkpoint=None):
    start, end = config.get("deployment_block"), head["number"]
    if start is None or not 0 <= start <= end:
        raise ValueError("deployment_block must be within the report block range")
    addresses = [Web3.to_checksum_address(config[name]) for name in EVENTS]
    source = dict(
        version=1, chain_id=client.eth.chain_id, addresses=addresses, start=start
    )
    saved = _load_checkpoint(checkpoint, source) if checkpoint is not None else None
    first, logs = start, []
    if saved is not None and start <= saved["block"] <= end:
        height = saved["block"]
        previous = head if height == end else client.eth.get_block(height)
        if Web3.to_hex(previous["hash"]) == saved["block_hash"]:
            first, logs = height + 1, saved["logs"]

    # Store the raw logs so that changes to the report decoder also apply to old events.
    timestamps = {}
    requests = 0
    for low in range(first, end + 1, CHUNK_SIZE):
        entries = client.eth.get_logs(
            {
                "address": addresses,
                "fromBlock": low,
                "toBlock": min(low + CHUNK_SIZE - 1, end),
            }
        )
        requests += 1
        for entry in entries:
            block_hash = entry["blockHash"]
            if block_hash not in timestamps:
                timestamps[block_hash] = client.eth.get_block(block_hash)["timestamp"]
            logs.append(
                dict(json.loads(Web3.to_json(entry)), timestamp=timestamps[block_hash])
            )

    if checkpoint is not None:
        print(
            f"Scanned {max(0, end - first + 1)} blocks through {end} "
            f"with {requests} eth_getLogs requests",
            file=sys.stderr,
        )
    return dict(
        source=source, block=end, block_hash=Web3.to_hex(head["hash"]), logs=logs
    )


def decode_history(logs, funding, proceeds):
    decoders = {}
    events = {}
    timestamps = {}
    for name, escrow in (("funding", funding), ("proceeds", proceeds)):
        for event_name in EVENTS[name]:
            event = escrow.events[event_name]()
            decoders[escrow.address, event.topic] = event
            events[event_name] = []
    for log in logs:
        if not log["topics"]:
            continue
        decoder = decoders.get((log["address"], log["topics"][0]))
        if decoder is not None:
            entry = decoder.process_log(
                {**log, "transactionHash": HexBytes(log["transactionHash"])}
            )
            events[entry["event"]].append(entry)
            timestamps[log["blockNumber"]] = log["timestamp"]
    return events, timestamps
