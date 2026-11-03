from __future__ import annotations
from eth_abi import decode
from web3 import Web3
from .core import Pending, address, check_price, contract, multicall


def _uint(raw):
    return decode(["uint256"], raw)[0]


class Chain:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.contracts = {
            n: contract(client, n, a)
            for n, a in config["addresses"].items()
            if n not in ["receiver", "convex_voter", "market"]
        }
        self.final = client.eth.get_block("finalized")
        self.number = self.final["number"]
        self._blocks = {}

    def read(self, name, method, *args, block=None):
        return (
            self.contracts[name]
            .functions[method](*args)
            .call(block_identifier=self.number if block is None else block)
        )

    def block(self, n):
        if n not in self._blocks:
            self._blocks[n] = self.client.eth.get_block(n)
        return self._blocks[n]

    def first_block(self, timestamp):
        if timestamp > self.final["timestamp"]:
            raise Pending("Week boundary is not finalized")
        low, high = 0, self.number
        while low < high:
            middle = (low + high) // 2
            if self.block(middle)["timestamp"] < timestamp:
                low = middle + 1
            else:
                high = middle
        return low

    def logs(self, event, start, end=None, filters=None):
        end = self.number if end is None else end
        result = []
        for first in range(start, end + 1, 2000):
            result.extend(
                event.get_logs(
                    from_block=first,
                    to_block=min(first + 1999, end),
                    argument_filters=filters or {},
                )
            )
        return result

    def snapshot(self, week):
        b = self.first_block(week)
        timestamp = self.block(b)["timestamp"]
        try:
            price = check_price(
                self.read("feed", "latestRoundData", block=b), timestamp
            )
        except ValueError as error:
            raise Pending(f"{error} at week {week}") from error
        receiver, gauge = (
            self.config["addresses"]["receiver"],
            self.config["addresses"]["gauge"],
        )
        accrued = self.read("minter", "minted", receiver, gauge, block=b)
        # Checkpoint and read in ONE eth_call: unfilled controller slots and a stale
        # gauge rate would otherwise read incorrectly.
        g, controller = (
            self.contracts["gauge"].functions,
            self.contracts["controller"].functions,
        )
        claimable, weight, relative, rate, epoch = multicall(
            self.client,
            [
                g.claimable_tokens_write(receiver),
                controller.points_weight(gauge, week),
                controller.gauge_relative_weight(gauge, week),
                g.inflation_rate(),
                g.future_epoch_time(),
            ],
            b,
        )
        return {
            "week": week,
            "block": b,
            "block_hash": Web3.to_hex(self.block(b)["hash"]),
            "timestamp": timestamp,
            "oracle_round": str(price[0]),
            "price": price[1],
            "price_updated_at": price[3],
            "accrued_crv": accrued + _uint(claimable),
            "total_weight": decode(["uint256", "uint256"], weight)[0],
            "relative_weight": _uint(relative),
            "inflation_rate": _uint(rate),
            "future_epoch_time": _uint(epoch),
            "killed": self.read("gauge", "is_killed", block=b),
        }

    def voter_weight(self, account, week):
        """Reconstruct the vote effective at the boundary, excluding new next-week votes."""
        # A vote in the boundary block schedules NEXT week and overwrites vote_user_slopes.
        b = self.first_block(week) - 1
        slope, power, end = self.read(
            "controller",
            "vote_user_slopes",
            address(account),
            self.config["addresses"]["gauge"],
            block=b,
        )
        return slope * max(end - week, 0)


def verify_escrow(client, c):
    e = contract(client, "escrow", c["escrow"])
    expected = {
        "TOKEN": "usdc",
        "GAUGE": "gauge",
        "VOTIUM": "votium",
        "REMOTE": "remote",
        "VOTE_MARKET": "market",
        "RECEIVER": "receiver",
    }
    for getter, key in expected.items():
        if e.functions[getter]().call() != c["addresses"][key]:
            raise ValueError(f"Unexpected escrow {getter}")
    for getter, key in [
        ("TREASURY", "treasury"),
        ("MANAGEMENT", "management"),
        ("CAMPAIGN_MANAGER", "campaign_manager"),
    ]:
        if e.functions[getter]().call() != address(c[key]):
            raise ValueError(f"Unexpected escrow {getter}")
    if e.functions.MAX_REWARD_PER_VOTE().call() != int(c["max_reward_per_vote"]):
        raise ValueError("Unexpected vote cap")
    return e
