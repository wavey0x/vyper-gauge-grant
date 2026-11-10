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
from vyper_grant.cli import setup, doctor, deployment, main
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
    governance = ADDRESSES["governance"]
    for account in [manager, receiver, governance]:
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
        if transaction["from"] in [
            bytes.fromhex(a[2:]) for a in [manager, receiver, governance]
        ]:
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

    usdc, crv, crvusd, vault, gauge, controller, minter, pool, stable_pool = [
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
    s["tester"].time_travel(s["start"] - 180)
    s["escrow"].functions.sell(0).transact({"from": s["keeper"]})
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
    assert not s["escrow"].functions.weeks(s["start"]).call()[0]
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["spent"] == str(1000 * 10**6)
    assert int(data["totals"]["yearn"]) > 0
    assert data["proceeds_balance"] == data["totals"]["yearn"]
    s["escrow"].functions.recover(
        s["usdc"].address, int(data["proceeds_balance"])
    ).transact({"from": s["manager"]})
    recovered = report(s["client"], s["c"])
    assert recovered["proceeds_balance"] == "0"
    assert recovered["totals"] == data["totals"]
    assert recovered["withdrawn_usdc"] == data["proceeds_balance"]


def test_receiver_routing_and_report_distinguish_forwarded_from_held(chain, tmp_path):
    s = chain
    begin(s)
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    held = s["usdc"].functions.balanceOf(s["escrow"].address).call()
    s["escrow"].functions.set_yearn_receiver(s["payee"]).transact(
        {"from": s["manager"]}
    )
    before = s["usdc"].functions.balanceOf(s["payee"]).call()
    s["tester"].time_travel(s["start"] + 80000)
    s["tester"].mine_block()
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    data = report(s["client"], s["c"])
    forwarded = s["usdc"].functions.balanceOf(s["payee"]).call() - before
    assert forwarded > 0
    assert data["yearn_receiver"] == s["payee"]
    assert data["proceeds_balance"] == str(held)
    assert data["forwarded_usdc"] == str(forwarded)
    assert int(data["totals"]["yearn"]) == held + forwarded


def test_cli_prepares_receiver_change_and_governance_clawback(
    chain, tmp_path, monkeypatch
):
    s = chain
    config = tmp_path / "config.json"
    config.write_text(json.dumps(s["c"]))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])

    def prepare(*args):
        monkeypatch.setattr(
            "sys.argv",
            ["vyper-grant", "--config", str(config), "--out", str(tmp_path), *args],
        )
        main()
        return json.loads((tmp_path / (args[0] + ".json")).read_text())

    change = prepare("set-yearn-receiver", "--account", s["payee"])
    assert change["meta"]["safeAddress"] == s["manager"]
    s["client"].eth.send_transaction(
        {
            "from": s["manager"],
            **{k: change["transactions"][0][k] for k in ["to", "data"]},
        }
    )
    assert s["escrow"].functions.yearn_receiver().call() == s["payee"]
    # Governance clawback must work without a deployed or configured seller.
    config.write_text(json.dumps({**s["c"], "proceeds": None}))
    shares = s["vault"].functions.balanceOf(s["funding"].address).call()
    recovery = prepare(
        "recover",
        "funding",
        "--governance",
        "--token",
        s["vault"].address,
        "--amount-raw",
        str(shares),
    )
    assert recovery["meta"]["safeAddress"] == s["governance"]
    s["client"].eth.send_transaction(
        {
            "from": s["governance"],
            **{k: recovery["transactions"][0][k] for k in ["to", "data"]},
        }
    )
    assert s["vault"].functions.balanceOf(s["governance"]).call() == shares


def test_checkpoint_retries_while_paused_and_explicit_command_is_public(
    chain, tmp_path
):
    s = chain
    s["funding"].functions.purchase(s["start"], 1).transact({"from": s["manager"]})
    s["escrow"].functions.set_paused(True).transact({"from": s["manager"]})
    s["tester"].time_travel(s["start"] + 1)
    s["tester"].mine_block()
    first = run(s["client"], s["c"], directory=tmp_path)
    assert first["status"] == "checkpoint"
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "checkpoint"
    submit(s, first)
    assert s["escrow"].functions.weeks(s["start"]).call()[0]
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "paused"
    s["escrow"].functions.set_keeper(s["keeper"], False).transact(
        {"from": s["manager"]}
    )
    explicit = run(s["client"], s["c"], directory=tmp_path, checkpoint_only=True)
    submit(s, explicit)
    assert explicit["status"] == "checkpoint"


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


def test_closing_sale_bypasses_minimum_and_sells_whole_claim(chain, tmp_path):
    s = chain
    begin(s)
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    s["c"]["min_trade_crv"] = "1000000"
    s["tester"].time_travel(s["start"] + WEEK - 600)
    s["tester"].mine_block()
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "below minimum"
    s["tester"].time_travel(s["start"] + WEEK - 180)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and int(result["crv"]) > 1000 * 10**18
    tx = submit(s, result)
    receipt = s["client"].eth.get_transaction_receipt(tx)
    assert receipt.status == 1
    assert s["crv"].functions.balanceOf(s["receiver"]).call() == 0
    assert (
        s["gauge"].functions.integrate_fraction(s["receiver"]).call()
        == s["minter"].functions.minted(s["receiver"], s["gauge"].address).call()
    )


def test_unfunded_sales_are_reported_with_zero_yearn_share(chain, tmp_path):
    s = chain
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale"
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["spent"] == data["totals"]["yearn"] == "0"
    assert int(data["totals"]["sold"]) > 0
    assert data["weeks"][0]["status"] == "Vyper only"
    assert "earned" not in data["totals"] and "remaining" not in data["weeks"][0]


def test_programme_end_tail_is_sold_for_vyper_below_minimum(chain, tmp_path):
    s = chain
    end = s["escrow"].functions.END().call()
    s["tester"].time_travel(end - 180)
    s["escrow"].functions.sell(0).transact({"from": s["keeper"]})
    s["c"]["min_trade_crv"] = "1000000"
    s["tester"].time_travel(end + 60)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale"
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["weeks"][-1]["week"] == str(end)
    assert data["weeks"][-1]["yearn"] == "0"


def test_uncertain_broadcast_uses_chain_state_on_next_pass(
    chain, tmp_path, monkeypatch
):
    s = chain
    begin(s)
    monkeypatch.setenv(
        "KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex()
    )
    nonce = s["client"].eth.get_transaction_count(s["keeper"])
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
    assert s["client"].eth.get_transaction_count(s["keeper"]) == nonce + 1
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
