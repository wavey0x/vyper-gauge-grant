"""Exercise the real Python workflow against deployed contracts on an in-memory EVM."""

import json
import boa
import pytest
from eth_tester import EthereumTester, PyEVMBackend
from eth.vm.spoof import SpoofTransaction
from web3 import Web3, EthereumTesterProvider
from web3.exceptions import ContractLogicError
from conftest import deploy_mocks
from vyper_grant.core import ADDRESSES, ROOT, WEEK, artifact
from vyper_grant.chain import verify, report, historical_vote
from vyper_grant.chain import enabled_keepers
from vyper_grant.cli import setup, doctor, deployment, main
from vyper_grant import keeper as keeper_module
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
    # The keeper's wall clock follows the test chain; runs are unscheduled unless a tick is given.
    monkeypatch.setattr(
        "vyper_grant.keeper.wall_clock",
        lambda: client.eth.get_block("latest").timestamp + 1,
    )
    monkeypatch.delenv("KEEPER_TICK", raising=False)
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
        c[name + "_tx"] = Web3.to_hex(receipt.transactionHash)
        return client.eth.contract(
            address=receipt.contractAddress, abi=artifact(name)["abi"]
        )

    usdc, crv, crvusd, vault, gauge, controller, minter, pool, stable_pool, feed = [
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
    s["funding"].functions.fund_round(2000 * 10**6).transact(
        {"from": s["manager"]}
    )
    s["tester"].time_travel(s["start"] - 180)
    quoted = s["escrow"].functions.claimable().call()
    s["escrow"].functions.work(
        0, quoted, True
    ).transact({"from": s["keeper"]})
    s["tester"].time_travel(s["start"] + 40000)
    s["tester"].mine_block()
    s["funding"].functions.work().transact({"from": s["keeper"]})


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
    assert len(vyper["transactions"]) == 2 and len(manager["transactions"]) == 1
    assert vyper["meta"]["mintEnabledAtRead"] is True
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
    assert arguments["min_out"] == int(result["min_usdc"])
    assert not s["escrow"].functions.weeks(s["start"]).call()[0]
    submit(s, result)
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "cooldown"
    data = report(s["client"], s["c"])
    assert data["spent"] == str(1000 * 10**6)
    assert "budget" not in data
    assert int(data["totals"]["yearn"]) > 0
    assert data["proceeds_balance"] == data["totals"]["yearn"]
    s["crvusd"].functions.mint(s["escrow"].address, 7 * 10**18).transact()
    s["escrow"].functions.withdraw(
        s["crvusd"].address, 7 * 10**18, s["payee"]
    ).transact({"from": s["manager"]})
    assert report(s["client"], s["c"])["withdrawn_usdc"] == "0"
    s["escrow"].functions.withdraw(
        s["usdc"].address, int(data["proceeds_balance"]), s["payee"]
    ).transact({"from": s["manager"]})
    withdrawn = report(s["client"], s["c"])
    assert withdrawn["proceeds_balance"] == "0"
    assert withdrawn["totals"] == data["totals"]
    assert withdrawn["withdrawn_usdc"] == data["proceeds_balance"]
    assert s["usdc"].functions.balanceOf(s["payee"]).call() == int(data["proceeds_balance"])


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
    assert args["min_out"] == int(quoted["usdc"]) * 995 // 1000
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
    s["crv"].functions.mint(s["receiver"], 100 * 10**18).transact()
    s["tester"].time_travel(s["start"] - 300)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path, tick=s["start"] - 300)
    assert result["status"] == "retain" and result["closing"]
    assert result["swap_crv"] == result["min_usdc"] == "0"
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
        s["usdc"].functions.mint(s["escrow"].address, 123 * 10**6).transact()
        withdrawal = prepare(
            "withdraw", "--token", s["usdc"].address,
            "--amount-raw", str(123 * 10**6), "--recipient", s["payee"],
            *(["--governance"] if role == "governance" else []),
        )
        assert withdrawal["meta"]["safeAddress"] == s[role]
        s["client"].eth.send_transaction(
            {
                "from": s[role],
                **{k: withdrawal["transactions"][0][k] for k in ["to", "data"]},
            }
        )
    assert s["usdc"].functions.balanceOf(s["payee"]).call() == 246 * 10**6
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


def test_funding_cli_and_report_record_scheduled_payments(chain, tmp_path, monkeypatch):
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
        sender = s["governance"] if "--governance" in args else s["manager"]
        assert prepared["meta"]["safeAddress"] == sender
        tx = prepared["transactions"][0]
        s["client"].eth.send_transaction({
            "from": sender, "to": tx["to"], "data": tx["data"]
        })

    burner = s["funding"].functions.recipient().call()
    execute("fund-round", "--usdc", "30")
    pending = report(s["client"], s["c"])
    assert pending["spent"] == "0"
    assert pending["funding_next_week"] == str(s["start"])
    assert pending["funding_payments"] == [str(15 * 10**6)] * 2
    assert run(s["client"], s["c"], directory=tmp_path, funding_only=True)["status"] == "not due"
    s["tester"].time_travel(s["start"])
    s["tester"].mine_block()
    payment = run(s["client"], {**s["c"], "proceeds": None}, directory=tmp_path, funding_only=True)
    assert payment["status"] == "funding payment" and payment["usdc"] == str(15 * 10**6)
    submit(s, payment)
    execute("set-recipient", "--account", s["payee"])
    s["tester"].time_travel(s["start"] + WEEK)
    s["tester"].mine_block()
    submit(s, run(s["client"], s["c"], directory=tmp_path, funding_only=True))
    assert s["usdc"].functions.balanceOf(burner).call() == 15 * 10**6
    assert s["usdc"].functions.balanceOf(s["payee"]).call() == 15 * 10**6
    data = report(s["client"], s["c"])
    assert data["spent"] == str(30 * 10**6)
    assert all(row["sold"] == "0" for row in data["weeks"])
    assert [p["recipient"] for p in data["payments"]] == [burner, s["payee"]]
    for payment in data["payments"]:
        receipt = s["client"].eth.get_transaction_receipt(payment["tx"])
        assert int(payment["timestamp"]) == s["client"].eth.get_block(receipt.blockNumber).timestamp
        assert "week" not in payment
    assert data["funding_recipient"] == s["payee"]
    assert data["funding_next_week"] == "0" and data["funding_payments"] == ["0", "0"]
    execute("fund-round", "--usdc", "40")
    execute("cancel-round", "--governance")
    assert not s["funding"].functions.canWork().call()
    assert s["funding"].functions.payments(0).call() == 0


def test_funding_keeper_cli_works_without_proceeds_or_keeper_role(chain, tmp_path, monkeypatch, capsys):
    s = chain
    s["funding"].functions.fund_round(20 * 10**6).transact({"from": s["manager"]})
    s["escrow"].functions.set_keeper(s["keeper"], False).transact({"from": s["manager"]})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({**s["c"], "proceeds": None}))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])
    monkeypatch.setattr("sys.argv", [
        "vyper-grant", "--config", str(config), "--out", str(tmp_path), "funding-keeper",
    ])
    s["tester"].time_travel(s["start"])
    s["tester"].mine_block()
    main()
    prepared = json.loads(capsys.readouterr().out)
    function, args = s["funding"].decode_function_input(prepared["data"])
    assert function.fn_name == "work" and args == {}
    submit(s, prepared)
    main()
    assert json.loads(capsys.readouterr().out)["status"] == "not due"


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
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 600)
    s["tester"].mine_block()
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "hold"
    s["tester"].time_travel(boundary - 300)
    s["tester"].mine_block()
    # An unscheduled run never gets the closing waiver.
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "hold"
    result = run(s["client"], s["c"], directory=tmp_path, tick=boundary - 300)
    assert result["status"] == "sale" and result["closing"]
    assert int(result["crv"]) > 1000 * 10**18
    tx = submit(s, result)
    receipt = s["client"].eth.get_transaction_receipt(tx)
    assert receipt.status == 1
    assert s["client"].eth.get_block(receipt.blockNumber).timestamp < boundary
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
    # The contract has no week argument. Only the keeper's pre-signing recheck stops a
    # sale prepared before the boundary from being sent after it.
    s = chain
    begin(s)
    s["escrow"].functions.checkpoint().transact()
    s["gauge"].functions.set_vote(
        s["voter"], 2 * 10**12, s["start"] + 52 * WEEK, s["start"] + 40000
    ).transact()
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 300)
    s["tester"].mine_block()
    prepared = run(s["client"], s["c"], directory=tmp_path, tick=boundary - 300)
    assert prepared["status"] == "sale"
    s["tester"].time_travel(boundary + 60)
    receipt = s["client"].eth.get_transaction_receipt(submit(s, prepared))
    sold = s["escrow"].events.Sold().get_logs(
        from_block=receipt.blockNumber, to_block=receipt.blockNumber
    )[0]["args"]
    assert sold["week"] == boundary
    assert sold["yearn"] == sold["usdc"] // 2
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


def set_allowance(s, amount):
    crv = s["crv"].functions
    crv.approve(s["escrow"].address, 0).transact({"from": s["receiver"]})
    crv.approve(s["escrow"].address, amount).transact({"from": s["receiver"]})


def selector(fn):
    return fn._encode_transaction_data()[:10]


def called(tx):
    data = tx["data"]
    return (data if isinstance(data, str) else Web3.to_hex(data))[:10].lower()


def fail_calls(s, monkeypatch, *fns, error=None):
    """Make eth_call revert for the given contract functions, as a broken pool or state would."""
    original = s["client"].eth.call
    blocked = {selector(fn) for fn in fns}

    def call(tx, *args, **kwargs):
        if called(tx) in blocked:
            raise error or ContractLogicError("execution reverted: pool")
        return original(tx, *args, **kwargs)

    monkeypatch.setattr(s["client"].eth, "call", call)


def test_allowance_caps_sales_and_exhaustion_is_reported(chain, tmp_path):
    s = chain
    begin(s)
    s["c"].update(min_trade_crv="10")
    set_allowance(s, 50 * 10**18)
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and result["crv"] == str(50 * 10**18)
    assert result["allowance"] == str(50 * 10**18) and result["mint_enabled"]
    assert s["client"].eth.get_transaction_receipt(submit(s, result)).status == 1
    s["tester"].time_travel(s["client"].eth.get_block("latest").timestamp + 600)
    s["tester"].mine_block()
    assert run(s["client"], s["c"], directory=tmp_path) == {
        "status": "allowance exhausted", "allowance": "0", "mint_enabled": True,
    }
    # A positive allowance below the minimum still funds the closing sale.
    set_allowance(s, 5 * 10**18)
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 300)
    s["tester"].mine_block()
    closing = run(s["client"], s["c"], directory=tmp_path, tick=boundary - 300)
    assert closing["status"] == "sale" and closing["crv"] == str(5 * 10**18)


def test_mint_off_sizes_on_safe_balance_without_claimable(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    s["c"].update(min_trade_crv="10")
    s["minter"].functions.toggle_approve_mint(s["escrow"].address).transact({"from": s["receiver"]})
    s["crv"].functions.mint(s["receiver"], 30 * 10**18).transact()
    balance = s["crv"].functions.balanceOf(s["receiver"]).call()
    assert s["escrow"].functions.claimable().call() > balance
    calls = []
    original = s["client"].eth.call
    monkeypatch.setattr(
        s["client"].eth, "call",
        lambda tx, *a, **k: calls.append(called(tx)) or original(tx, *a, **k),
    )
    result = run(s["client"], s["c"], directory=tmp_path)
    assert selector(s["escrow"].functions.claimable()) not in calls
    assert result["status"] == "sale" and result["crv"] == str(balance)
    assert result["mint_enabled"] is False
    assert s["client"].eth.get_transaction_receipt(submit(s, result)).status == 1


@pytest.mark.parametrize("amount, raw_quote", [(8 * 10**11, None), (10**12, 0), (4 * 10**12, 1)])
def test_dust_skips_the_sale_but_records_a_due_snapshot(chain, tmp_path, amount, raw_quote):
    s = chain
    begin(s)
    if raw_quote is not None:
        assert s["escrow"].functions.quote(amount).call() == raw_quote
    set_allowance(s, amount)
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "checkpoint" and result["sale"] == "dust"
    submit(s, result)
    assert s["escrow"].functions.weeks(s["start"]).call()[0]
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "dust"


def test_quote_revert_falls_back_to_checkpoint_and_cli_exits_3(chain, tmp_path, monkeypatch, capsys):
    s = chain
    begin(s)
    fail_calls(s, monkeypatch, s["escrow"].functions.quote(0))
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "simulation failed" and result["step"] == "quote"
    assert result["reason"] == "contract revert"
    assert result["fallback"] == "checkpoint"
    assert result["data"] == s["escrow"].functions.checkpoint()._encode_transaction_data()
    submit(s, result)
    assert s["escrow"].functions.weeks(s["start"]).call()[0]
    config = tmp_path / "config.json"
    config.write_text(json.dumps(s["c"]))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])
    monkeypatch.setattr("sys.argv", ["vyper-grant", "--config", str(config), "--out", str(tmp_path), "keeper"])
    with pytest.raises(SystemExit) as exit:
        main()
    assert exit.value.code == 3
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "simulation failed" and "fallback" not in printed


def test_failed_sale_and_failed_fallback_report_checkpoint_failed(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    f = s["escrow"].functions
    fail_calls(s, monkeypatch, f.work(0, 0, True), f.checkpoint())
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "checkpoint failed" and result["step"] == "simulate"
    assert result["fallback_reason"] == "contract revert"


def test_transport_errors_are_not_treated_as_reverts(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    fail_calls(s, monkeypatch, s["escrow"].functions.quote(0), error=ConnectionError("rpc down"))
    with pytest.raises(ConnectionError):
        run(s["client"], s["c"], directory=tmp_path)


def test_hold_before_the_close_still_records_a_due_snapshot(chain, tmp_path):
    s = chain
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 600)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "checkpoint" and result["sale"] == "hold"
    submit(s, result)
    assert run(s["client"], s["c"], directory=tmp_path)["status"] == "hold"


def test_send_rechecks_the_week_after_estimation(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 300)
    s["tester"].mine_block()
    times = [boundary - 299]
    monkeypatch.setattr("vyper_grant.keeper.wall_clock", lambda: times.pop(0) if times else boundary + 1)
    nonce = s["client"].eth.get_transaction_count(s["keeper"])
    assert run(s["client"], s["c"], True, tmp_path, tick=boundary - 300)["status"] == "boundary"
    assert s["client"].eth.get_transaction_count(s["keeper"]) == nonce


def test_closing_send_adds_boundary_gas_and_checks_balance(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    boundary = s["start"] + WEEK
    s["tester"].time_travel(boundary - 300)
    s["tester"].mine_block()
    original = s["client"].eth.get_balance
    monkeypatch.setattr(s["client"].eth, "get_balance", lambda account, *a: 0)
    assert run(s["client"], s["c"], True, tmp_path, tick=boundary - 300)["status"] == "low balance"
    monkeypatch.setattr(s["client"].eth, "get_balance", original)
    sent = run(s["client"], s["c"], True, tmp_path, tick=boundary - 300)
    tx = s["client"].eth.get_transaction(sent["tx"])
    receipt = s["client"].eth.get_transaction_receipt(sent["tx"])
    assert receipt.status == 1 and tx["gas"] > receipt.gasUsed + 1_000_000


def head_at(s, timestamp):
    s["tester"].time_travel(timestamp)
    s["tester"].mine_block()
    assert s["client"].eth.get_block("latest").timestamp == timestamp


def test_funding_stays_off_the_nonce_until_the_new_week(chain, tmp_path):
    s = chain
    s["funding"].functions.fund_round(20 * 10**6).transact({"from": s["manager"]})
    boundary = s["start"] + WEEK
    c = {**s["c"], "proceeds": None}
    for head, clock in [(boundary - 601, boundary - 600), (boundary - 301, boundary - 300), (boundary - 1, boundary + 1)]:
        head_at(s, head)
        # The first instalment is overdue, so only the window keeps funding quiet.
        assert s["funding"].functions.canWork().call()
        status = run(s["client"], c, directory=tmp_path, funding_only=True, clock=lambda: clock)
        assert status["status"] == "closing window"
    head_at(s, boundary + 11)
    assert run(s["client"], c, directory=tmp_path, funding_only=True)["status"] == "funding payment"


@pytest.mark.parametrize("cooldown", [600, 1200])
def test_close_then_new_week_checkpoint_then_first_sale(chain, tmp_path, cooldown):
    s = chain
    begin(s)
    s["escrow"].functions.set_cooldown(cooldown).transact({"from": s["manager"]})
    boundary = s["start"] + WEEK
    head_at(s, boundary - 301)
    close = run(s["client"], s["c"], directory=tmp_path, tick=boundary - 300)
    assert close["status"] == "sale" and close["closing"]
    receipt = s["client"].eth.get_transaction_receipt(submit(s, close))
    assert s["client"].eth.get_block(receipt.blockNumber).timestamp < boundary
    s["crv"].functions.mint(s["receiver"], 1000 * 10**18).transact()
    head_at(s, boundary + 299)
    first = run(s["client"], s["c"], directory=tmp_path, tick=boundary + 300)
    assert first["status"] == "checkpoint"
    submit(s, first)
    assert s["escrow"].functions.weeks(boundary).call()[0]
    first_sale = boundary + (600 if cooldown == 600 else 1200)
    for tick in range(boundary + 600, first_sale + 1, 300):
        head_at(s, tick - 1)
        status = run(s["client"], s["c"], directory=tmp_path, tick=tick)["status"]
        assert status == ("sale" if tick == first_sale else "cooldown")


def test_send_discards_a_sale_prepared_in_an_earlier_week(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    boundary = s["start"] + WEEK
    head_at(s, boundary - 1201)
    real = keeper_module.timing
    later = {"block": 0, "ts": boundary + 11, "clock": boundary + 12,
             "head_left": WEEK - 11, "clock_left": WEEK - 12, "head_age": 1}
    calls = []

    def timing(client, clock):
        calls.append(1)
        return real(client, clock) if len(calls) == 1 else later

    # An unscheduled run whose head and clock both move into the next week mid-preparation.
    monkeypatch.setattr(keeper_module, "timing", timing)
    nonce = s["client"].eth.get_transaction_count(s["keeper"])
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "boundary"
    assert s["client"].eth.get_transaction_count(s["keeper"]) == nonce


def test_sent_fallback_checkpoint_records_the_week(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    fail_calls(s, monkeypatch, s["escrow"].functions.quote(0))
    result = run(s["client"], s["c"], True, tmp_path)
    assert result["status"] == "simulation failed" and result["fallback"] == "checkpoint"
    assert s["client"].eth.get_transaction_receipt(result["tx"]).status == 1
    assert s["escrow"].functions.weeks(s["start"]).call()[0]


def test_explicit_checkpoint_captures_the_first_new_week_head(chain, tmp_path):
    s = chain
    boundary = s["start"] + WEEK
    head_at(s, boundary - 1)
    # The old-week head with a new-week clock is not current yet.
    assert run(
        s["client"], s["c"], directory=tmp_path, checkpoint_only=True,
        clock=lambda: boundary + 1,
    )["status"] == "boundary"
    head_at(s, boundary + 11)
    result = run(s["client"], s["c"], directory=tmp_path, checkpoint_only=True, tick=boundary)
    assert result["status"] == "checkpoint"
    submit(s, result)
    assert s["escrow"].functions.weeks(boundary).call()[0]


def test_cooldown_raised_during_preparation_holds_the_sale(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    boundary = s["start"] + WEEK
    head_at(s, boundary - 1201)
    real = keeper_module.timing
    calls = []

    def timing(client, clock):
        calls.append(1)
        if len(calls) == 2:
            # The manager's cooldown increase confirms while the 23:40 sale is being prepared.
            s["escrow"].functions.set_cooldown(1200).transact({"from": s["manager"]})
        return real(client, clock)

    monkeypatch.setattr(keeper_module, "timing", timing)
    nonce = s["client"].eth.get_transaction_count(s["keeper"])
    assert run(s["client"], s["c"], True, tmp_path)["status"] == "hold"
    assert s["client"].eth.get_transaction_count(s["keeper"]) == nonce


def test_final_decision_uses_a_clock_read_after_the_cooldown_call(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    boundary = s["start"] + WEEK
    head_at(s, boundary - 49)
    # Preparation at W-48, final timing at W-37, then one second passes during the cooldown read.
    times = [boundary - 48, boundary - 37, boundary - 35]
    nonce = s["client"].eth.get_transaction_count(s["keeper"])
    result = run(s["client"], s["c"], True, tmp_path, tick=boundary - 300, clock=lambda: times.pop(0))
    assert result["status"] == "boundary"
    assert s["client"].eth.get_transaction_count(s["keeper"]) == nonce


def execute_batch(s, prepared):
    for tx in prepared["transactions"]:
        s["client"].eth.send_transaction(
            {"from": prepared["meta"]["safeAddress"], "to": tx["to"], "data": tx["data"]}
        )


def test_setup_and_allowance_batches_rerun_over_a_used_allowance(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)  # a sale has consumed part of the allowance
    vyper, _ = setup(s["client"], s["c"], 400_000 * 10**18)
    execute_batch(s, vyper)
    allowance = s["crv"].functions.allowance(s["receiver"], s["escrow"].address).call()
    assert allowance == 400_000 * 10**18
    config = tmp_path / "config.json"
    config.write_text(json.dumps(s["c"]))
    monkeypatch.setattr("vyper_grant.cli.rpc", lambda: s["client"])
    monkeypatch.setattr("sys.argv", [
        "vyper-grant", "--config", str(config), "--out", str(tmp_path), "allowance", "--crv", "1000",
    ])
    main()
    top_up = json.loads((tmp_path / "allowance-vyper.safe.json").read_text())
    assert top_up["meta"]["allowanceAtRead"] == str(400_000 * 10**18)
    assert len(top_up["transactions"]) == 2
    assert "toggle" not in json.dumps(top_up)
    execute_batch(s, top_up)
    assert s["crv"].functions.allowance(s["receiver"], s["escrow"].address).call() == 1000 * 10**18


def test_toggle_follows_a_fresh_read_and_doctor_flags_an_operating_gap(chain):
    s = chain
    s["minter"].functions.toggle_approve_mint(s["escrow"].address).transact({"from": s["receiver"]})
    vyper, _ = setup(s["client"], s["c"], 10**24)
    assert vyper["meta"]["mintEnabledAtRead"] is False and len(vyper["transactions"]) == 3
    assert "problems" not in doctor(s["client"], s["c"])
    report_ = doctor(s["client"], {**s["c"], "operating": True})
    assert report_["problems"] == ["Vyper's mint permission is off"]
    assert report_["creation_check"] == {"funding": "creation transaction", "proceeds": "creation transaction"}
    execute_batch(s, vyper)
    assert doctor(s["client"], {**s["c"], "operating": True})["problems"] == []


def test_replacement_pauses_and_revokes_every_old_permission(chain):
    s = chain
    old = s["escrow"]
    other = s["client"].eth.accounts[6]
    old.functions.set_keeper(other, True).transact({"from": s["manager"]})
    gone = s["client"].eth.accounts[7]
    old.functions.set_keeper(gone, True).transact({"from": s["manager"]})
    old.functions.set_keeper(gone, False).transact({"from": s["manager"]})
    new = s["deploy"]("proceeds")
    with pytest.raises(ValueError, match="must differ"):
        setup(s["client"], s["c"], 10**24, new.address, 1)
    with pytest.raises(ValueError, match="replace-block"):
        setup(s["client"], s["c"], 10**24, old.address)
    vyper, manager = setup(s["client"], s["c"], 10**24, old.address, 1)
    execute_batch(s, manager)
    execute_batch(s, vyper)
    assert old.functions.paused().call()
    assert enabled_keepers(s["client"], old, 1) == []
    assert s["crv"].functions.allowance(s["receiver"], old.address).call() == 0
    assert not s["minter"].functions.allowed_to_mint_for(old.address, s["receiver"]).call()
    assert s["crv"].functions.allowance(s["receiver"], new.address).call() == 10**24
    assert s["minter"].functions.allowed_to_mint_for(new.address, s["receiver"]).call()
    assert new.functions.keepers(s["keeper"]).call()
    # Rerunning after both batches builds no further changes to the old escrow.
    vyper, manager = setup(s["client"], s["c"], 10**24, old.address, 1)
    assert len(manager["transactions"]) == 1 and len(vyper["transactions"]) == 3


def test_historical_vote_search_is_bounded_and_checked(chain):
    s = chain
    s["tester"].time_travel(s["start"] + 100)
    s["tester"].mine_block()
    final = s["client"].eth.get_block("finalized")
    assert historical_vote(s["client"], s["start"], final, 1)["slope"] == 10**12
    with pytest.raises(ValueError, match="multiple"):
        historical_vote(s["client"], s["start"] + 1, final, 1)
    first = next(
        n for n in range(final["number"] + 1)
        if s["client"].eth.get_block(n)["timestamp"] >= s["start"]
    )
    with pytest.raises(ValueError, match="deployment_block"):
        historical_vote(s["client"], s["start"], final, first + 50_400)


def test_proceeds_deployment_gas_includes_the_weekly_update_once(chain):
    s = chain
    for name, extra in [("funding", 0), ("proceeds", 1_000_000)]:
        tx = deployment(s["client"], {}, name, s["deployer"])["transaction"]
        estimate = s["client"].eth.estimate_gas(
            {"from": s["deployer"], "data": artifact(name)["bytecode"]}
        )
        assert tx["gas"] == estimate * 12 // 10 + extra


def test_creation_transaction_is_checked_where_configured(chain, tmp_path):
    s = chain
    verify(s["client"], s["c"])
    swapped = {**s["c"], "proceeds_tx": s["c"]["funding_tx"]}
    with pytest.raises(ValueError, match="proceeds creation"):
        verify(s["client"], swapped, names=("proceeds",))
    with pytest.raises(ValueError, match="proceeds creation"):
        run(s["client"], swapped, directory=tmp_path)
    legacy = {k: v for k, v in s["c"].items() if not k.endswith("_tx")}
    verify(s["client"], legacy)
    assert doctor(s["client"], legacy)["creation_check"]["proceeds"] == "runtime only"


def test_cli_errors_print_only_owned_messages(chain, tmp_path, monkeypatch, capsys):
    s = chain
    config = tmp_path / "config.json"
    config.write_text(json.dumps(s["c"]))

    def leaky():
        raise ConnectionError("HTTPConnectionPool: Max retries exceeded with url: /v2/SECRETKEY123")

    monkeypatch.setattr("vyper_grant.cli.rpc", leaky)
    monkeypatch.setattr("sys.argv", ["vyper-grant", "--config", str(config), "doctor"])
    with pytest.raises(SystemExit):
        main()
    err = capsys.readouterr().err
    assert "SECRETKEY123" not in err and err.strip() == "Error: ConnectionError"


def test_doctor_verifies_proceeds_without_a_keeper(chain):
    s = chain
    status = doctor(s["client"], {**s["c"], "keeper": None})
    assert status["creation_check"]["proceeds"] == "creation transaction"
    assert status["keeper_enabled"] is False
    with pytest.raises(ValueError, match="proceeds creation"):
        doctor(s["client"], {**s["c"], "keeper": None, "proceeds_tx": s["c"]["funding_tx"]})


def deploy_guard(s):
    prepared = deployment(s["client"], s["c"], "guard", s["deployer"])
    receipt = s["client"].eth.wait_for_transaction_receipt(
        s["client"].eth.send_transaction(prepared["transaction"])
    )
    s["c"].update(guard=receipt.contractAddress, guard_tx=Web3.to_hex(receipt.transactionHash))
    return s["client"].eth.contract(address=receipt.contractAddress, abi=artifact("guard")["abi"])


def test_keeper_sells_through_the_guard_after_setup(chain, tmp_path, monkeypatch):
    s = chain
    begin(s)
    guard = deploy_guard(s)
    verify(s["client"], s["c"], names=("guard",))
    _, manager = setup(s["client"], s["c"], 10**24)
    calls = [s["escrow"].decode_function_input(t["data"])[1] for t in manager["transactions"]]
    assert calls == [
        {"account": guard.address, "enabled": True},
        {"account": s["keeper"], "enabled": False},
    ]
    execute_batch(s, manager)
    status = doctor(s["client"], {**s["c"], "operating": True})
    assert status["guard_enabled"] and not status["keeper_enabled"] and status["problems"] == []
    s["tester"].time_travel(s["client"].eth.get_block("latest").timestamp + 600)
    s["tester"].mine_block()
    result = run(s["client"], s["c"], directory=tmp_path)
    assert result["status"] == "sale" and result["to"] == guard.address
    function, arguments = guard.decode_function_input(result["data"])
    assert function.fn_name == "work" and arguments["max_crv"] == int(result["crv"])
    assert s["client"].eth.get_transaction_receipt(submit(s, result)).status == 1
    # The signed path sends to the guard too.
    monkeypatch.setenv("KEEPER_PRIVATE_KEY", s["tester"].backend.account_keys[4].to_hex())
    s["crv"].functions.mint(s["receiver"], 1000 * 10**18).transact()
    s["tester"].time_travel(s["client"].eth.get_block("latest").timestamp + 600)
    s["tester"].mine_block()
    sent = run(s["client"], s["c"], True, tmp_path)
    assert sent["status"] == "sale"
    assert s["client"].eth.get_transaction(sent["tx"])["to"] == guard.address
    assert s["client"].eth.get_transaction_receipt(sent["tx"]).status == 1
    # Any other enabled keeper is a problem once operating.
    s["escrow"].functions.set_keeper(s["payee"], True).transact({"from": s["manager"]})
    problems = doctor(s["client"], {**s["c"], "operating": True})["problems"]
    assert problems == ["Accounts other than the guard are enabled keepers"]


def test_guard_needs_sell_mode_and_matching_creation(chain, tmp_path):
    s = chain
    deploy_guard(s)
    s["escrow"].functions.set_keeper(s["c"]["guard"], True).transact({"from": s["manager"]})
    with pytest.raises(ValueError, match="sell_vyper"):
        run(s["client"], {**s["c"], "sell_vyper": False}, directory=tmp_path)
    with pytest.raises(ValueError, match="sell_vyper"):
        setup(s["client"], {**s["c"], "sell_vyper": False}, 10**24)
    # The explicit public checkpoint still works when only the sale route is unusable.
    s["tester"].time_travel(s["start"] + 100)
    s["tester"].mine_block()
    explicit = run(s["client"], {**s["c"], "sell_vyper": False}, directory=tmp_path, checkpoint_only=True)
    assert explicit["status"] == "checkpoint" and explicit["to"] == s["escrow"].address
    with pytest.raises(ValueError, match="guard BOT"):
        verify(s["client"], {**s["c"], "keeper": s["payee"]}, names=("guard",))
    with pytest.raises(ValueError, match="guard creation"):
        verify(s["client"], {**s["c"], "guard_tx": s["c"]["proceeds_tx"]}, names=("guard",))
