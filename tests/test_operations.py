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

    usdc, crv, crvusd, vault, gauge, controller, minter, pool = [
        client.eth.contract(address=str(mock.address), abi=mock.abi)
        for mock in mocks.values()
    ]
    start = (client.eth.get_block("latest").timestamp // WEEK + 1) * WEEK
    c = json.loads((ROOT / "config/example.json").read_text())
    c.update(
        keeper=keeper,
        deployment_block=1,
    )
    escrow = deploy("proceeds")
    funding = deploy("funding")
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
    s["funding"].functions.draw(1000 * 10**6).transact(
        {"from": s["manager"]}
    )
    s["tester"].time_travel(s["start"] - 180)
    quoted = s["escrow"].functions.claimable().call()
    s["escrow"].functions.work(
        0, quoted, True
    ).transact({"from": s["keeper"]})
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()


def submit(s, result):
    return s["client"].eth.send_transaction(
        {"from": s["keeper"], "to": result["to"], "data": result["data"]}
    )


def test_verified_contracts_and_idempotent_setup(chain):
    s = chain
    verify(s["client"], s["c"])
    verify(s["client"], {"funding": s["funding"].address}, names=("funding",))
    prepared = deployment(s["client"], {}, "funding", s["deployer"])
    assert prepared["constructor_arguments"] == []
    vyper, manager = setup(s["client"], {**s["c"], "funding": None}, 10**25)
    assert len(vyper["transactions"]) == len(manager["transactions"]) == 1
    assert doctor(s["client"], s["c"])["mint_enabled"]
    assert doctor(s["client"], {**s["c"], "funding": None})["mint_enabled"]
    assert s["escrow"].functions.DEPLOYED_WEEK().call() == s["start"] - WEEK
    with pytest.raises(ValueError, match="bytecode"):
        verify(s["client"], {**s["c"], "funding": s["gauge"].address})


def test_keeper_prepares_one_sale_and_report_tracks_withdrawals(chain, tmp_path):
    s = chain
    begin(s)
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and result["dry_run"]
    function, arguments = s["escrow"].decode_function_input(result["data"])
    assert function.fn_name == "work"
    assert set(arguments) == {"min_out", "max_crv", "sell_vyper"}
    assert arguments["sell_vyper"] is True
    assert arguments["max_crv"] == int(result["max_crv"])
    assert arguments["max_crv"] == int(result["crv"])
    assert arguments["min_out"] == int(result["min_crvusd"])
    assert not s["escrow"].functions.weeks(s["start"]).call()[0]
    submit(s, result)
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "cooldown"
    data = report(s["client"], s["c"])
    assert data["spent"] == str(1000 * 10**6)
    assert "budget" not in data
    assert int(data["totals"]["yearn"]) > 0
    assert data["proceeds_balance"] == data["totals"]["yearn"]
    s["usdc"].functions.mint(s["escrow"].address, 7 * 10**6).transact()
    s["escrow"].functions.withdraw(
        s["usdc"].address, 7 * 10**6, s["payee"]
    ).transact({"from": s["manager"]})
    assert report(s["client"], s["c"])["withdrawn_crvusd"] == "0"
    s["escrow"].functions.withdraw(
        s["crvusd"].address, int(data["proceeds_balance"]), s["payee"]
    ).transact({"from": s["manager"]})
    withdrawn = report(s["client"], s["c"])
    assert withdrawn["proceeds_balance"] == "0"
    assert withdrawn["totals"] == data["totals"]
    assert withdrawn["withdrawn_crvusd"] == data["proceeds_balance"]
    assert s["crvusd"].functions.balanceOf(s["payee"]).call() == int(data["proceeds_balance"])


def test_receiver_routing_and_report_distinguish_forwarded_from_held(chain, tmp_path):
    s = chain
    begin(s)
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    held = s["crvusd"].functions.balanceOf(s["escrow"].address).call()
    s["escrow"].functions.set_yearn_receiver(s["payee"]).transact(
        {"from": s["manager"]}
    )
    before = s["crvusd"].functions.balanceOf(s["payee"]).call()
    s["tester"].time_travel(s["start"] + 80000)
    s["tester"].mine_block()
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    data = report(s["client"], s["c"])
    forwarded = s["crvusd"].functions.balanceOf(s["payee"]).call() - before
    assert forwarded > 0
    assert data["yearn_receiver"] == s["payee"]
    assert data["proceeds_balance"] == str(held)
    assert data["forwarded_crvusd"] == str(forwarded)
    assert int(data["totals"]["yearn"]) == held + forwarded


def test_keeper_quotes_exact_capped_pull_including_receiver_balance(chain, tmp_path):
    s = chain
    begin(s)
    s["c"].update(max_crv="100", min_trade_crv="50")
    s["crv"].functions.mint(s["receiver"], 500 * 10**18).transact()
    result = run(s["client"], s["c"], directory=tmp_path)
    _, args = s["escrow"].decode_function_input(result["data"])
    amount = 100 * 10**18
    assert args["max_crv"] == int(result["crv"]) == amount
    assert args["min_out"] == s["escrow"].functions.quote(amount).call() * 995 // 1000
    receipt = s["client"].eth.get_transaction_receipt(submit(s, result))
    sold = s["escrow"].events.Sold().get_logs(
        from_block=receipt.blockNumber, to_block=receipt.blockNumber
    )[0]["args"]
    assert sold["crv"] == amount
    assert s["crv"].functions.balanceOf(s["receiver"]).call() >= 400 * 10**18


def test_keeper_quotes_only_yearns_crv_and_reports_retention(chain, tmp_path, monkeypatch, capsys):
    s = chain
    begin(s)
    s["c"].update(sell_vyper=False, max_crv="100", min_trade_crv="50")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(s["c"]))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])
    monkeypatch.setattr("sys.argv", ["vyper-grant", "--config", str(config), "quote"])
    main()
    quoted = json.loads(capsys.readouterr().out)
    result = run(s["client"], s["c"], directory=tmp_path)
    _, args = s["escrow"].decode_function_input(result["data"])
    assert args["sell_vyper"] is False
    assert args["max_crv"] == 100 * 10**18
    assert quoted["swap_crv"] == result["swap_crv"] == str(25 * 10**18)
    assert quoted["vyper_crv"] == result["vyper_crv"] == str(75 * 10**18)
    assert args["min_out"] == int(quoted["crvusd"]) * 995 // 1000
    assert not s["escrow"].functions.weeks(s["start"]).call()[0]
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["weeks"][-1]["sold"] == str(25 * 10**18)
    assert data["weeks"][-1]["yearn"] == data["weeks"][-1]["output"]
    assert data["totals"]["vyper_crv"] == data["vyper_crv_balance"] == str(75 * 10**18)
    assert data["withdrawn_crv"] == "0"
    s["tester"].time_travel(s["client"].eth.get_block("latest").timestamp + 600)
    s["tester"].mine_block()
    s["c"]["sell_vyper"] = True
    submit(s, run(s["client"], s["c"], directory=tmp_path))
    assert report(s["client"], s["c"])["vyper_crv_balance"] == str(75 * 10**18)


def test_keeper_can_retain_entire_batch_with_zero_yearn_share(chain, tmp_path):
    s = chain
    s["c"].update(sell_vyper=False, max_crv="100", min_trade_crv="50")
    s["tester"].time_travel(s["start"] - 180)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "retain"
    assert result["swap_crv"] == result["min_crvusd"] == "0"
    assert result["vyper_crv"] == result["crv"] == str(100 * 10**18)
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["vyper_crv_balance"] == str(100 * 10**18)
    assert data["totals"]["sold"] == data["totals"]["output"] == "0"
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "cooldown"


def test_cli_prepares_receiver_change_withdrawal_and_governance_clawback(
    chain, tmp_path, monkeypatch
):
    s = chain
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**s["c"], "funding": None}))
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
    change = prepare("set-cooldown", "--seconds", "900")
    assert change["meta"]["safeAddress"] == s["manager"]
    s["client"].eth.send_transaction(
        {
            "from": s["manager"],
            **{k: change["transactions"][0][k] for k in ["to", "data"]},
        }
    )
    assert s["escrow"].functions.cooldown().call() == 900
    with pytest.raises(SystemExit):
        prepare("set-cooldown", "--seconds", "599")
    for role in ["manager", "governance"]:
        s["crvusd"].functions.mint(s["escrow"].address, 123 * 10**18).transact()
        withdrawal = prepare(
            "withdraw", "--token", s["crvusd"].address,
            "--amount-raw", str(123 * 10**18), "--recipient", s["payee"],
            *(["--governance"] if role == "governance" else []),
        )
        assert withdrawal["meta"]["safeAddress"] == s[role]
        s["client"].eth.send_transaction(
            {
                "from": s[role],
                **{k: withdrawal["transactions"][0][k] for k in ["to", "data"]},
            }
        )
    assert s["crvusd"].functions.balanceOf(s["payee"]).call() == 246 * 10**18
    assert s["escrow"].functions.yearn_receiver().call() == s["payee"]
    s["crv"].functions.mint(s["escrow"].address, 10 * 10**18).transact()
    withdrawal = prepare(
        "withdraw", "--token", s["crv"].address,
        "--amount-raw", str(3 * 10**18), "--recipient", s["payee"],
    )
    assert withdrawal["meta"]["safeAddress"] == s["receiver"]
    s["client"].eth.send_transaction({
        "from": s["receiver"],
        **{k: withdrawal["transactions"][0][k] for k in ["to", "data"]},
    })
    data = report(s["client"], s["c"])
    assert data["withdrawn_crv"] == str(3 * 10**18)
    assert data["vyper_crv_balance"] == str(7 * 10**18)
    with pytest.raises(SystemExit):
        prepare(
            "withdraw", "--token", s["crv"].address,
            "--amount-raw", "1", "--recipient", s["payee"], "--governance",
        )
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
    s["c"]["funding"] = None
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


def test_draw_cli_and_report_record_each_recipient(chain, tmp_path, monkeypatch):
    s = chain
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**s["c"], "proceeds": None}))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])

    def execute(*args):
        monkeypatch.setattr("sys.argv", [
            "vyper-grant", "--config", str(config), "--out", str(tmp_path), *args
        ])
        main()
        prepared = json.loads((tmp_path / (args[0] + ".json")).read_text())
        assert prepared["meta"]["safeAddress"] == s["manager"]
        tx = prepared["transactions"][0]
        s["client"].eth.send_transaction({
            "from": s["manager"], "to": tx["to"], "data": tx["data"]
        })

    burner = s["funding"].functions.recipient().call()
    execute("draw", "--usdc", "10")
    execute("set-recipient", "--account", s["payee"])
    execute("draw", "--usdc", "20")
    assert s["usdc"].functions.balanceOf(burner).call() == 10 * 10**6
    assert s["usdc"].functions.balanceOf(s["payee"]).call() == 20 * 10**6
    data = report(s["client"], s["c"])
    assert data["spent"] == str(30 * 10**6)
    assert all(row["sold"] == "0" for row in data["weeks"])
    assert [p["recipient"] for p in data["payments"]] == [burner, s["payee"]]
    for payment in data["payments"]:
        receipt = s["client"].eth.get_transaction_receipt(payment["tx"])
        assert int(payment["timestamp"]) == s["client"].eth.get_block(receipt.blockNumber).timestamp
        assert "week" not in payment
    assert data["funding_recipient"] == s["payee"]


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


def test_closing_sale_bypasses_minimum_but_respects_cap(chain, tmp_path):
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
    sold = s["escrow"].events.Sold().get_logs(from_block=receipt.blockNumber, to_block=receipt.blockNumber)[0]["args"]["crv"]
    assert sold == int(result["crv"]) <= int(s["c"]["max_crv"]) * 10**18
    assert s["crv"].functions.balanceOf(s["receiver"]).call() > 0
    assert (
        s["gauge"].functions.integrate_fraction(s["receiver"]).call()
        == s["minter"].functions.minted(s["receiver"], s["gauge"].address).call()
    )


def test_keeper_and_weekly_reporting_work_without_payments(chain, tmp_path):
    s = chain
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()
    before = report(s["client"], s["c"])
    assert before["weeks"][-1]["status"] == "Needs checkpoint"
    assert before["warnings"] == ["The current week needs a vote snapshot"]
    result = run(s["client"], {**s["c"], "funding": None}, directory=tmp_path)
    assert result["status"] == "sale"
    submit(s, result)
    data = report(s["client"], s["c"])
    assert data["spent"] == "0" and data["payments"] == []
    assert int(data["totals"]["yearn"]) > 0
    assert int(data["totals"]["sold"]) > 0
    assert data["weeks"][-1]["status"] == "Active"
    assert "paid" not in data["weeks"][-1]
    assert "earned" not in data["totals"] and "remaining" not in data["weeks"][-1]


def test_boundary_checkpoint_during_cooldown_does_not_bypass_next_week_minimum(chain, tmp_path):
    s = chain
    begin(s)
    end = s["start"] + WEEK
    s["tester"].time_travel(end - 180)
    quoted = s["escrow"].functions.claimable().call()
    s["escrow"].functions.work(
        0, quoted, True
    ).transact({"from": s["keeper"]})
    s["c"]["min_trade_crv"] = "1000000"
    s["tester"].time_travel(end + 60)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "checkpoint"
    submit(s, result)
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "cooldown"
    s["tester"].time_travel(end + 420)
    s["tester"].mine_block()
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "below minimum"


def test_prepared_keeper_sale_can_land_in_next_week(chain, tmp_path):
    s = chain
    begin(s)
    s["escrow"].functions.checkpoint().transact()
    s["gauge"].functions.set_vote(
        s["voter"], 2 * 10**12, s["start"] + 52 * WEEK, s["start"] + 40000
    ).transact()
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 180)
    s["tester"].mine_block()
    prepared = run(s["client"], s["c"], directory=tmp_path)
    assert prepared["status"] == "sale"
    s["tester"].time_travel(boundary + 60)
    receipt = s["client"].eth.get_transaction_receipt(submit(s, prepared))
    sold = s["escrow"].events.Sold().get_logs(
        from_block=receipt.blockNumber, to_block=receipt.blockNumber
    )[0]["args"]
    assert sold["week"] == boundary
    assert sold["yearn"] == sold["crvusd"] // 2
    assert sold["crv"] == int(prepared["max_crv"])


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
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "cooldown"
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
