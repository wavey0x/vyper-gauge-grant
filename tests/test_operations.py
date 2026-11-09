"""Exercise the real Python workflow against deployed contracts on an in-memory EVM."""

import json
from functools import lru_cache
import pytest
from eth_tester import EthereumTester
from web3 import Web3, EthereumTesterProvider
from vyper import compile_code
from vyper_grant.core import ADDRESSES, ROOT, WEEK, artifact
from vyper_grant.chain import verify, reconcile, historical_vote
from vyper_grant.cli import setup, doctor
from vyper_grant.keeper import run


@lru_cache
def mock_artifact(name):
    return compile_code(
        (ROOT / "tests/mocks" / f"{name}.vy").read_text(),
        output_formats=["abi", "bytecode"],
    )


@pytest.fixture
def chain(monkeypatch):
    tester = EthereumTester()
    client = Web3(EthereumTesterProvider(tester))
    deployer, manager, receiver, voter, keeper, payee = client.eth.accounts[:6]
    client.eth.default_account = deployer

    def deploy(compiled, *args):
        factory = client.eth.contract(
            abi=compiled["abi"], bytecode=compiled["bytecode"]
        )
        receipt = client.eth.wait_for_transaction_receipt(
            factory.constructor(*args).transact()
        )
        return client.eth.contract(address=receipt.contractAddress, abi=compiled["abi"])

    usdc, crv, usd = [deploy(mock_artifact("Token")) for _ in range(3)]
    vault = deploy(mock_artifact("Vault"), usdc.address)
    gauge = deploy(mock_artifact("System"), crv.address, receiver)
    pool = deploy(mock_artifact("Pool"), crv.address, usd.address)
    start = (client.eth.get_block("latest").timestamp // WEEK + 1) * WEEK
    funding = deploy(artifact("funding"), vault.address, manager, payee, start, 4)
    escrow = deploy(
        artifact("proceeds"),
        funding.address,
        crv.address,
        usd.address,
        gauge.address,
        gauge.address,
        gauge.address,
        voter,
        pool.address,
    )
    c = json.loads((ROOT / "config/example.json").read_text())
    c.update(
        funding=funding.address,
        proceeds=escrow.address,
        payee=payee,
        start=start,
        weeks=4,
        keeper=keeper,
        deployment_block=1,
    )
    for key, val in dict(
        manager=manager,
        usdc=usdc.address,
        vault=vault.address,
        crv=crv.address,
        crvusd=usd.address,
        gauge=gauge.address,
        receiver=receiver,
        voter=voter,
        controller=gauge.address,
        minter=gauge.address,
        pool=pool.address,
    ).items():
        monkeypatch.setitem(ADDRESSES, key, val)
    usdc.functions.mint(manager, 100_000 * 10**6).transact()
    usdc.functions.approve(vault.address, 100_000 * 10**6).transact({"from": manager})
    vault.functions.deposit(100_000 * 10**6, funding.address).transact(
        {"from": manager}
    )
    gauge.functions.toggle_approve_mint(escrow.address).transact({"from": receiver})
    crv.functions.approve(escrow.address, 10**30).transact({"from": receiver})
    escrow.functions.set_keeper(keeper, True).transact({"from": manager})
    escrow.functions.set_limits(1000 * 10**18, 0, 200).transact({"from": manager})
    for w in range(start - WEEK, start + 5 * WEEK, WEEK):
        gauge.functions.set_weight(
            w, 4 * (start + 52 * WEEK - w) * 10**12, 10**16 + 17
        ).transact()
    gauge.functions.set_vote(voter, 10**12, start + 52 * WEEK, start - 1).transact()
    return dict(locals())


def activate(s):
    s["funding"].functions.purchase(
        s["start"], 1000 * 10**6, b"proof".ljust(32, b"\0")
    ).transact({"from": s["manager"]})
    s["escrow"].functions.activate().transact({"from": s["manager"]})
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()


def test_verified_contracts_and_idempotent_setup(chain):
    s = chain
    funding, escrow = verify(s["client"], s["c"])
    assert (
        funding.address == s["funding"].address
        and escrow.address == s["escrow"].address
    )
    vyper, manager = setup(s["client"], s["c"], 10**25)
    assert (
        len(vyper["transactions"]) == 1
    )  # No toggle that would revoke existing permission.
    assert len(manager["transactions"]) == 3
    assert doctor(s["client"], s["c"])["mint_enabled"]
    bad = {**s["c"], "payee": s["keeper"]}
    with pytest.raises(ValueError, match="PAYEE"):
        verify(s["client"], bad)
    bad = {**s["c"], "funding": s["gauge"].address}
    with pytest.raises(ValueError, match="bytecode"):
        verify(s["client"], bad)


def test_keeper_simulation_and_full_report_survive_recovery(chain, tmp_path):
    s = chain
    activate(s)
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "checkpoint" and result["dry_run"]
    s["client"].eth.send_transaction(
        {"from": s["keeper"], "to": result["to"], "data": result["data"]}
    )
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale"
    s["client"].eth.send_transaction(
        {"from": s["keeper"], "to": result["to"], "data": result["data"]}
    )
    report = reconcile(s["client"], s["c"])
    assert report["spent"] == str(1000 * 10**6)
    assert int(report["totals"]["retained"]) > 0
    assert int(report["proceeds_balance"]) == int(report["totals"]["retained"])
    s["escrow"].functions.recover(
        s["usd"].address, int(report["proceeds_balance"])
    ).transact({"from": s["manager"]})
    recovered = reconcile(s["client"], s["c"])
    assert recovered["proceeds_balance"] == "0"
    assert recovered["totals"] == report["totals"]
    assert recovered["withdrawn_crvusd"] == report["proceeds_balance"]


def test_archive_read_excludes_boundary_block_revote(chain):
    s = chain
    s["tester"].time_travel(s["start"])
    tx = (
        s["gauge"]
        .functions.set_vote(s["voter"], 2 * 10**12, s["start"] + 52 * WEEK, s["start"])
        .transact()
    )
    block = s["client"].eth.get_block(
        s["client"].eth.get_transaction_receipt(tx).blockNumber
    )
    assert block.timestamp == s["start"]
    evidence = historical_vote(
        s["client"], s["start"], s["client"].eth.get_block("finalized")
    )
    assert evidence["slope"] == 10**12
    assert evidence["block"] == block.number - 1


def test_reconciliation_detects_false_manager_recovery(chain):
    s = chain
    activate(s)
    s["gauge"].functions.set_vote(
        s["voter"], 2 * 10**12, s["start"] + 52 * WEEK, s["start"]
    ).transact()
    s["escrow"].functions.repair_week(
        s["start"], 0, 0, 0, b"bad".ljust(32, b"\0")
    ).transact({"from": s["manager"]})
    s["escrow"].functions.checkpoint().transact()
    with pytest.raises(ValueError, match="historical state"):
        reconcile(s["client"], s["c"])


def test_week_tail_below_minimum_still_finalizes(chain, tmp_path):
    s = chain
    activate(s)
    s["escrow"].functions.checkpoint().transact()
    # Isolate the final unpaid interval: the keeper must still close the programme.
    end = s["funding"].functions.END().call()
    s["tester"].time_travel(end - 10)
    s["escrow"].functions.checkpoint().transact()
    s["tester"].time_travel(end + 10)
    # Existing booked CRV is sold first; then the final checkpoint is prepared.
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale"
    s["client"].eth.send_transaction(
        {"from": s["keeper"], "to": result["to"], "data": result["data"]}
    )
    # Drain the remaining lot in bounded trades.
    for _ in range(10):
        result = run(s["client"], s["c"], directory=tmp_path)
        if result["status"] == "checkpoint":
            break
        assert result["status"] == "sale"
        s["client"].eth.send_transaction(
            {"from": s["keeper"], "to": result["to"], "data": result["data"]}
        )
    assert result["status"] == "checkpoint"


def test_keeper_signed_journal_recovers_uncertain_broadcast(
    chain, tmp_path, monkeypatch
):
    s = chain
    activate(s)
    # Expose mainnet chain ID for signing; all execution stays in the local EVM.
    monkeypatch.setattr(type(s["client"].eth), "chain_id", property(lambda _: 1))
    monkeypatch.setenv(
        "KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex()
    )
    original = s["client"].eth.send_raw_transaction
    sent = []

    def uncertain(raw):
        sent.append(bytes(raw))
        raise ConnectionError("connection lost before response")

    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", uncertain)
    with pytest.raises(ConnectionError):
        run(s["client"], s["c"], True, tmp_path)
    saved = json.loads((tmp_path / "pending.json").read_text())
    assert saved["hash"] == Web3.to_hex(Web3.keccak(sent[0]))
    assert bytes.fromhex(saved["raw"]) == sent[0]
    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", original)
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "pending"
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "confirming"
    s["tester"].mine_blocks(2)
    result = run(s["client"], s["c"], True, tmp_path)
    assert result["status"] == "sale"
    second = json.loads((tmp_path / "pending.json").read_text())
    assert second["nonce"] == saved["nonce"] + 1
    assert s["escrow"].functions.total_sold().call() > 0


def test_keeper_rejects_changed_configuration_with_pending_transaction(
    chain, tmp_path, monkeypatch
):
    s = chain
    activate(s)
    monkeypatch.setattr(type(s["client"].eth), "chain_id", property(lambda _: 1))
    monkeypatch.setenv(
        "KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex()
    )
    run(s["client"], s["c"], True, tmp_path)
    changed = {**s["c"], "keeper": s["manager"]}
    with pytest.raises(ValueError, match="another keeper configuration"):
        run(s["client"], changed, True, tmp_path)
