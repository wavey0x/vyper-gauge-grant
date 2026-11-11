from pathlib import Path
from types import SimpleNamespace
import boa
import pytest
from vyper_grant.core import ADDRESSES as A

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


def deploy_mocks():
    """Install dependencies at mainnet addresses; escrow source stays unchanged."""

    def load(name, key, *args):
        return boa.load(
            str(ROOT / "tests/mocks" / f"{name}.vy"), *args, override_address=A[key]
        )

    return dict(
        usdc=load("Token", "usdc"),
        crv=load("Token", "crv"),
        crvusd=load("Token", "crvusd"),
        vault=load("Vault", "vault", A["usdc"]),
        gauge=load("System", "gauge", A["receiver"]),
        controller=load("Controller", "controller", A["gauge"]),
        minter=load("Minter", "minter", A["crv"]),
        pool=load("Pool", "pool", A["crv"], A["crvusd"]),
    )


@pytest.fixture
def system():
    manager, receiver, voter = A["manager"], A["receiver"], A["voter"]
    keeper, stranger = [boa.env.generate_address() for _ in range(2)]
    mocks = deploy_mocks()
    usdc, crv, crvusd, vault, gauge, controller, minter, pool = (
        mocks.values()
    )
    start = (boa.env.evm.patch.timestamp // WEEK + 1) * WEEK
    funding = boa.load(str(ROOT / "contracts/FundingEscrow.vy"))
    payee = funding.recipient()
    usdc.mint(manager, 100_000 * 10**6)
    with boa.env.prank(manager):
        usdc.approve(vault.address, 100_000 * 10**6)
        vault.deposit(100_000 * 10**6, funding.address)
    for w in range(start - WEEK, start + 10 * WEEK, WEEK):
        gauge.set_weight(w, 4 * (start + 52 * WEEK - w) * 10**12, 10**16 + 17)
    gauge.set_vote(voter, 0, 0, 0)
    s = SimpleNamespace(**locals())
    deploy_proceeds(s)
    gauge.set_vote(voter, 10**12, start + 52 * WEEK, start - 1)
    return s


def deploy_proceeds(s):
    s.escrow = boa.load(str(ROOT / "contracts/ProceedsEscrow.vy"))
    with boa.env.prank(s.manager):
        s.escrow.set_keeper(s.keeper, True)
    with boa.env.prank(s.receiver):
        s.crv.approve(s.escrow.address, 2_000_000 * UNIT)
        s.minter.toggle_approve_mint(s.escrow.address)
