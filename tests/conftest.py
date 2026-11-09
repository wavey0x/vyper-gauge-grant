from pathlib import Path
from types import SimpleNamespace
import boa
import pytest

ROOT = Path(__file__).resolve().parents[1]
WEEK = 604800
UNIT = 10**18


@pytest.fixture(autouse=True)
def isolated_chain():
    # Boa's fork changes PyEVM's shared state class, also used by eth-tester.
    state_type = boa.env.evm.vm.__class__._state_class
    database_type = state_type.account_db_class
    try:
        with boa.env.anchor():
            yield
    finally:
        state_type.account_db_class = database_type


def advance(timestamp):
    boa.env.time_travel(seconds=timestamp - boa.env.evm.patch.timestamp)


@pytest.fixture
def system():
    load = lambda name, *args: boa.load(str(ROOT / name), *args)
    manager, receiver, payee, voter, keeper, stranger = [
        boa.env.generate_address() for _ in range(6)
    ]
    usdc, crv, usd = [load("tests/mocks/Token.vy") for _ in range(3)]
    vault = load("tests/mocks/Vault.vy", usdc.address)
    gauge = load("tests/mocks/System.vy", crv.address, receiver)
    pool = load("tests/mocks/Pool.vy", crv.address, usd.address)
    start = (boa.env.evm.patch.timestamp // WEEK + 1) * WEEK
    funding = load(
        "contracts/FundingEscrow.vy", vault.address, manager, payee, start, 8
    )
    escrow = load(
        "contracts/ProceedsEscrow.vy",
        funding.address,
        crv.address,
        usd.address,
        gauge.address,
        gauge.address,
        gauge.address,
        voter,
        pool.address,
    )
    usdc.mint(manager, 100_000 * 10**6)
    with boa.env.prank(manager):
        usdc.approve(vault.address, 100_000 * 10**6)
        vault.deposit(100_000 * 10**6, funding.address)
        escrow.set_limits(10_000 * UNIT, 0, 200)
        escrow.set_keeper(keeper, True)
    with boa.env.prank(receiver):
        crv.approve(escrow.address, 2_000_000 * UNIT)
        gauge.toggle_approve_mint(escrow.address)
    for w in range(start - WEEK, start + 10 * WEEK, WEEK):
        gauge.set_weight(w, 4 * (start + 52 * WEEK - w) * 10**12, 10**16 + 17)
    gauge.set_vote(voter, 10**12, start + 52 * WEEK, start - 1)
    return SimpleNamespace(**locals())
