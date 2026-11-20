"""Keeper timing on mainnet's slot grid, the shared signer lock and the cron wrapper's exit status."""

import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from web3.exceptions import ContractLogicError
from vyper_grant import keeper
from vyper_grant.keeper import (
    WEEK,
    acquire,
    checkpoint_window,
    funding_window,
    gas_limit,
    proceeds_window,
    reason,
    run,
    scheduled_tick,
    sign,
)

ROOT = Path(__file__).resolve().parents[1]
W = 2968 * WEEK  # a Thursday 00:00 UTC boundary


def at(head, clock=None):
    """Timing as the keeper reads it: blocks land 1 s before each tick, cron starts on the tick."""
    clock = head + 1 if clock is None else clock
    return {
        "block": 1,
        "ts": head,
        "clock": clock,
        "head_left": WEEK - head % WEEK,
        "clock_left": WEEK - clock % WEEK,
        "head_age": clock - head,
    }


def outcome(window):
    skip, closing, may_sell = window
    return "skip" if skip else "close" if closing else "sell" if may_sell else "hold"


@pytest.mark.parametrize(
    "capture, expected",
    [
        (W - 331, None),
        (W - 330, W - 300),
        (W - 240, W - 300),
        (W - 239, None),
        (W - 31, None),
        (W - 30, W),
        (W + 60, W),
        (W + 61, None),
    ],
)
def test_only_bounded_launch_jitter_counts_as_a_scheduled_tick(capture, expected):
    assert scheduled_tick(capture) == expected


def test_tick_comes_only_from_the_wrapper(monkeypatch):
    monkeypatch.delenv("KEEPER_TICK", raising=False)
    assert scheduled_tick() is None
    monkeypatch.setenv("KEEPER_TICK", str(W - 299))
    assert scheduled_tick() == W - 300


# Offset of each on-time cron tick from the boundary, and the outcome per cooldown.
GRID = [
    (-1800, "sell", "sell"),
    (-1500, "sell", "hold"),
    (-1200, "sell", "hold"),
    (-900, "hold", "hold"),
    (-600, "hold", "hold"),
    (-300, "close", "close"),
    (0, "skip", "skip"),
    (300, "sell", "sell"),
    (600, "sell", "sell"),
]


@pytest.mark.parametrize("offset, short, long", GRID)
def test_on_time_cron_grid(offset, short, long):
    tick = W + offset
    t = at(tick - 1)
    assert outcome(proceeds_window(t, tick, 600)) == short
    assert outcome(proceeds_window(t, tick, 1200)) == long


def test_ordinary_sale_inclusion_leaves_the_close_clear():
    for cooldown in [600, 1200]:
        last = max(o for o, short, long in GRID if o < -300 and (short if cooldown == 600 else long) == "sell")
        # The sale lands at the next slot; the closing head is W-301.
        assert (W - 301) - (W + last + 11) >= cooldown


def test_late_23_55_job_that_reaches_the_new_week_is_discarded():
    assert outcome(proceeds_window(at(W + 11, W + 12), W - 300, 600)) == "skip"


def test_scheduled_00_00_job_is_skipped_even_with_a_new_week_head():
    assert outcome(proceeds_window(at(W + 11, W + 12), W, 600)) == "skip"


def test_ordinary_job_that_stalls_past_its_cutoff_holds():
    assert outcome(proceeds_window(at(W - 901, W - 899), W - 1200, 600)) == "hold"
    assert outcome(proceeds_window(at(W - 1501, W - 1499), W - 1800, 1200)) == "hold"


def test_closing_needs_a_fresh_head():
    assert outcome(proceeds_window(at(W - 337, W - 299), W - 300, 600)) == "hold"
    assert outcome(proceeds_window(at(W - 325, W - 290), W - 300, 600)) == "close"


def test_unscheduled_runs_never_close():
    assert outcome(proceeds_window(at(W - 301), None, 600)) == "hold"


def test_closing_window_ends_36_seconds_before_the_boundary():
    assert outcome(proceeds_window(at(W - 49, W - 37), W - 300, 600)) == "close"
    assert outcome(proceeds_window(at(W - 37, W - 36), W - 300, 600)) == "skip"


def test_head_from_the_future_is_clock_skew():
    assert proceeds_window(at(W - 1000, W - 1006), None, 600)[0] == "clock skew"
    assert funding_window(at(W - 1000, W - 1006)) == "clock skew"
    assert checkpoint_window(at(W - 1000, W - 1006)) == "clock skew"


def test_head_up_to_five_seconds_ahead_is_allowed():
    assert proceeds_window(at(W - 1000, W - 1005), None, 600)[0] is None
    assert funding_window(at(W - 1000, W - 1005)) is None
    assert checkpoint_window(at(W - 1000, W - 1005)) is None


def test_late_23_50_launch_is_relabelled_as_the_close():
    # A 23:50 job launched 4m30s late lands inside the close window; the real 23:55
    # job then sees the cooldown.
    tick = scheduled_tick(W - 330)
    assert tick == W - 300
    assert outcome(proceeds_window(at(W - 337, W - 329), tick, 600)) == "close"


def test_explicit_checkpoint_skips_the_schedule_but_needs_a_current_week_head():
    assert checkpoint_window(at(W - 1, W + 1)) == "boundary"
    assert checkpoint_window(at(W + 11, W + 12)) is None
    assert checkpoint_window(at(W - 301)) is None


def test_funding_is_quiet_from_23_50_until_a_new_week_head():
    assert funding_window(at(W - 901)) is None
    assert funding_window(at(W - 601)) == "closing window"
    assert funding_window(at(W - 301)) == "closing window"
    assert funding_window(at(W - 1, W + 1)) == "closing window"
    assert funding_window(at(W + 11)) is None


def test_funding_ignores_a_lagging_node():
    assert funding_window(at(W - 601, W + 12)) == "closing window"


def test_every_proceeds_transaction_carries_boundary_gas():
    assert gas_limit(500_000, True) == 1_700_000
    assert gas_limit(500_000, False) == 700_000


class FakeFunction:
    def build_transaction(self, tx):
        return {**tx, "gas": 500_000, "maxFeePerGas": 10**9}


class FakeClient:
    def __init__(self, balance):
        self.balance = balance
        self.eth = self
        self.account = self
        self.gas_price = 10**9

    def from_key(self, key):
        account = type("Account", (), {})()
        account.address = "0x" + "11" * 20
        account.sign_transaction = lambda tx: tx
        return account

    def get_balance(self, sender):
        return self.balance


def test_balance_must_cover_the_buffered_gas_limit(monkeypatch):
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", "unused")
    sender, c = "0x" + "11" * 20, {"max_gas_gwei": "20"}
    need = 1_700_000 * 10**9
    assert sign(FakeClient(need - 1), c, FakeFunction(), sender, 0, True) == {"status": "low balance"}
    assert sign(FakeClient(need), c, FakeFunction(), sender, 0, True)["gas"] == 1_700_000
    assert sign(FakeClient(700_000 * 10**9), c, FakeFunction(), sender, 0, False)["gas"] == 700_000


def test_revert_reasons_are_allowlisted():
    assert reason(ContractLogicError("execution reverted: missing vote history")) == (
        "execution reverted: missing vote history"
    )
    assert reason(ContractLogicError("execution reverted: stale")) == "execution reverted: stale"
    assert reason(ContractLogicError("execution reverted: TOKEN_12345")) == "contract revert"
    leaky = ContractLogicError("execution reverted: see https://rpc.example/v2/SECRET")
    assert reason(leaky) == "contract revert"


def hold_lock(path, seconds):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            "f = open(sys.argv[1], 'a')\n"
            "fcntl.flock(f, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(float(sys.argv[2]))\n",
            str(path),
            str(seconds),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "locked"
    return holder


def test_lock_waits_for_another_process_and_gives_up_at_the_deadline(tmp_path):
    path = tmp_path / "lock"
    holder = hold_lock(path, 1.5)
    with path.open("a") as lock:
        started = time.monotonic()
        assert not acquire(lock, wait=0.6)
        assert time.monotonic() - started >= 0.5
        assert acquire(lock, wait=5)
    holder.wait()


def test_run_reports_busy_without_touching_the_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(keeper, "LOCK_WAIT", 0.6)
    holder = hold_lock(tmp_path / "lock", 2)
    assert run(None, {}, directory=tmp_path) == {"status": "busy"}
    holder.wait()


@pytest.mark.parametrize("keeper_rc, report_rc, expected", [(0, 0, 0), (3, 0, 3), (3, 1, 3), (0, 1, 1)])
def test_wrapper_keeps_the_keeper_status_and_records_the_tick(tmp_path, keeper_rc, report_rc, expected):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$*" in *report*) exit "$REPORT_RC" ;; esac\n'
        'echo "$KEEPER_TICK" > "$VYPER_GRANT_DIR/tick"\n'
        'exit "$KEEPER_RC"\n'
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VYPER_GRANT_DIR": str(tmp_path),
        "KEEPER_RC": str(keeper_rc),
        "REPORT_RC": str(report_rc),
    }
    started = int(time.time())
    result = subprocess.run(["sh", str(ROOT / "ops/run.sh")], env=env)
    assert result.returncode == expected
    assert started <= int((tmp_path / "tick").read_text()) <= int(time.time())
