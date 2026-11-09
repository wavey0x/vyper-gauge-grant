"""Exercise the real Python workflow against deployed contracts on an in-memory EVM."""

import json
import boa
import pytest
from eth_tester import EthereumTester, PyEVMBackend
from eth.vm.spoof import SpoofTransaction
from web3 import Web3, EthereumTesterProvider
from conftest import deploy_mocks
from vyper_grant.core import ADDRESSES, ROOT, WEEK, artifact
from vyper_grant.chain import verify, report, historical_vote
from vyper_grant.cli import setup, doctor, deployment
from vyper_grant.keeper import run


@pytest.fixture
def chain(monkeypatch):
    # Seed dependencies at their fixed addresses without editing escrow bytecode.
    mocks = deploy_mocks()
    genesis = PyEVMBackend.generate_genesis_state()
    for mock in mocks.values():
        layout = mock.compiler_data.storage_layout["storage_layout"].values()
        slots = (
            slot
            for field in layout
            for slot in range(field["slot"], field["slot"] + field["n_slots"])
        )
        genesis[bytes.fromhex(str(mock.address)[2:])] = dict(
            balance=0,
            nonce=1,
            code=boa.env.get_code(mock.address),
            storage={slot: boa.env.get_storage(mock.address, slot) for slot in slots},
        )
    manager, receiver, voter = [ADDRESSES[k] for k in ["manager", "receiver", "voter"]]
    for account in [manager, receiver]:
        genesis[bytes.fromhex(account[2:])] = dict(
            balance=10**24, nonce=0, code=b"", storage={}
        )
    backend = PyEVMBackend(
        genesis_state=genesis,
        genesis_parameters={
            "timestamp": boa.env.evm.patch.timestamp,
            "gas_limit": 30_000_000,
        },
    )
    tester = EthereumTester(backend)
    client = Web3(EthereumTesterProvider(tester))
    monkeypatch.setattr(type(client.eth), "chain_id", property(lambda _: 1))
    deployer, keeper, payee = [client.eth.accounts[i] for i in [0, 4, 5]]
    client.eth.default_account = deployer
    original_sign = backend._get_normalized_and_signed_evm_transaction

    def sign(transaction, block_number="latest"):
        if transaction["from"] in [bytes.fromhex(a[2:]) for a in [manager, receiver]]:
            unsigned = backend._get_normalized_and_unsigned_evm_transaction(
                transaction, block_number
            )
            return SpoofTransaction(
                unsigned.as_signed_transaction(backend.account_keys[0]),
                sender=transaction["from"],
            )
        return original_sign(transaction, block_number)

    monkeypatch.setattr(backend, "_get_normalized_and_signed_evm_transaction", sign)

    def deploy(name):
        prepared = deployment(client, c, name, deployer)
        receipt = client.eth.wait_for_transaction_receipt(
            client.eth.send_transaction(prepared["transaction"])
        )
        c[name] = receipt.contractAddress
        return client.eth.contract(
            address=receipt.contractAddress, abi=artifact(name)["abi"]
        )

    usdc, crv, usd, vault, gauge, controller, minter, pool = [
        client.eth.contract(address=str(mock.address), abi=mock.abi)
        for mock in mocks.values()
    ]
    start = (client.eth.get_block("latest").timestamp // WEEK + 1) * WEEK
    c = json.loads((ROOT / "config/example.json").read_text())
    c.update(
        payee=payee,
        start=start,
        weeks=4,
        keeper=keeper,
        deployment_block=1,
    )
    funding = deploy("funding")
    escrow = deploy("proceeds")
    usdc.functions.mint(manager, 100_000 * 10**6).transact()
    usdc.functions.approve(vault.address, 100_000 * 10**6).transact({"from": manager})
    vault.functions.deposit(100_000 * 10**6, funding.address).transact(
        {"from": manager}
    )
    minter.functions.toggle_approve_mint(escrow.address).transact({"from": receiver})
    crv.functions.approve(escrow.address, 10**30).transact({"from": receiver})
    escrow.functions.set_keeper(keeper, True).transact({"from": manager})
    for w in range(start - WEEK, start + 5 * WEEK, WEEK):
        gauge.functions.set_weight(
            w, 4 * (start + 52 * WEEK - w) * 10**12, 10**16 + 17
        ).transact()
    gauge.functions.set_vote(voter, 10**12, start + 52 * WEEK, start - 1).transact()
    return dict(locals())


def begin(s):
    s["funding"].functions.purchase(s["start"], 1000 * 10**6).transact(
        {"from": s["manager"]}
    )
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()


def submit(s, result):
    return s["client"].eth.send_transaction(
        {"from": s["keeper"], "to": result["to"], "data": result["data"]}
    )


def test_verified_contracts_and_idempotent_setup(chain):
    s = chain
    verify(s["client"], s["c"])
    vyper, manager = setup(s["client"], s["c"], 10**25)
    assert len(vyper["transactions"]) == len(manager["transactions"]) == 1
    assert doctor(s["client"], s["c"])["mint_enabled"]
    with pytest.raises(ValueError, match="PAYEE"):
        verify(s["client"], {**s["c"], "payee": s["keeper"]})
    with pytest.raises(ValueError, match="bytecode"):
        verify(s["client"], {**s["c"], "funding": s["gauge"].address})


def test_keeper_prepares_one_sale_and_report_survives_recovery(chain, tmp_path):
    s = chain
    begin(s)
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and result["dry_run"]
    assert (
        s["escrow"].functions.available().call() == 0
    )  # Preview did not persist sync.
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["spent"] == str(1000 * 10**6)
    assert int(data["totals"]["retained"]) > 0
    assert data["proceeds_balance"] == data["totals"]["retained"]
    s["escrow"].functions.recover(
        s["usd"].address, int(data["proceeds_balance"])
    ).transact({"from": s["manager"]})
    recovered = report(s["client"], s["c"])
    assert recovered["proceeds_balance"] == "0"
    assert recovered["totals"] == data["totals"]
    assert recovered["withdrawn_crvusd"] == data["proceeds_balance"]


def test_boundary_sync_retries_and_works_while_paused(chain, tmp_path):
    s = chain
    s["funding"].functions.purchase(s["start"], 1).transact({"from": s["manager"]})
    s["escrow"].functions.set_paused(True).transact({"from": s["manager"]})
    s["tester"].time_travel(s["start"] - 299)
    s["tester"].mine_block()
    first = run(s["client"], s["c"], directory=tmp_path)
    assert first["status"] == "sync"
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "sync"
    submit(s, first)
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "paused"
    s["tester"].time_travel(s["start"] + 1)
    s["tester"].mine_block()
    after = run(s["client"], s["c"], directory=tmp_path)
    assert after["status"] == "sync"
    submit(s, after)
    assert s["escrow"].functions.weeks(s["start"]).call()[0]
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "paused"


def test_archive_read_excludes_boundary_block_revote(chain):
    s = chain
    s["tester"].time_travel(s["start"])
    tx = (
        s["gauge"]
        .functions.set_vote(s["voter"], 2 * 10**12, s["start"] + 52 * WEEK, s["start"])
        .transact()
    )
    assert (
        s["client"]
        .eth.get_block(s["client"].eth.get_transaction_receipt(tx).blockNumber)
        .timestamp
        == s["start"]
    )
    historical = historical_vote(
        s["client"], s["start"], s["client"].eth.get_block("finalized")
    )
    assert historical["slope"] == 10**12


def test_final_tail_syncs_then_sells_below_normal_minimum(chain, tmp_path):
    s = chain
    begin(s)
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    s["c"]["min_trade_crv"] = "1000"
    end = s["funding"].functions.END().call()
    # First funded week has a small unsold tail; the final programme sync is still due.
    s["tester"].time_travel(s["start"] + WEEK + 1)
    s["escrow"].functions.sync().transact()
    available = s["escrow"].functions.available().call()
    s["escrow"].functions.sell(available - 10**18, 0).transact({"from": s["keeper"]})
    s["tester"].time_travel(end + 1)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and result["crv"] == str(10**18)
    submit(s, result)
    assert s["escrow"].functions.cursor().call() >= end
    assert s["escrow"].functions.available().call() == 0


def test_uncertain_broadcast_uses_chain_state_on_next_pass(
    chain, tmp_path, monkeypatch
):
    s = chain
    begin(s)
    monkeypatch.setenv(
        "KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex()
    )
    original = s["client"].eth.send_raw_transaction
    hashes = []

    def uncertain(raw):
        hashes.append(original(raw))
        raise ConnectionError("response lost after inclusion")

    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", uncertain)
    with pytest.raises(ConnectionError):
        run(s["client"], s["c"], True, tmp_path)
    assert s["client"].eth.get_transaction_receipt(hashes[0]).status == 1
    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", original)
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "below minimum"
    assert s["client"].eth.get_transaction_count(s["keeper"]) == 1
    assert not (tmp_path / "pending.json").exists()


def test_pending_nonce_prevents_another_submission(chain, tmp_path, monkeypatch):
    s = chain
    original = s["client"].eth.get_transaction_count
    monkeypatch.setattr(
        s["client"].eth,
        "get_transaction_count",
        lambda account, block_identifier="latest": (
            original(account) + (block_identifier == "pending")
        ),
    )
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "pending nonce"
