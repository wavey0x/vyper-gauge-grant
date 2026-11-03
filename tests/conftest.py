from pathlib import Path
import boa
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_chain():
    with boa.env.anchor():
        boa.env.time_travel(
            seconds=1793730000 - boa.env.evm.patch.timestamp
            if boa.env.evm.patch.timestamp < 1793730000
            else 0
        )
        yield


@pytest.fixture
def system():
    treasury, management, receiver, arb, stranger = [
        boa.env.generate_address() for _ in range(5)
    ]
    token = boa.load(str(ROOT / "tests/mocks/Token.vy"))
    source = boa.load(str(ROOT / "tests/mocks/System.vy"), token.address, receiver)
    escrow = boa.load(
        str(ROOT / "contracts/VyperFundingEscrow.vy"),
        token.address,
        treasury,
        management,
        source.address,
        source.address,
        source.address,
        stranger,
        arb,
        100_000,
    )
    token.mint(escrow.address, 100_000 * 10**6)
    boa.env.set_balance(management, 10**18)
    return escrow, token, source, treasury, management, receiver, arb, stranger
