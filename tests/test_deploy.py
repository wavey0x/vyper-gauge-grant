"""Deployment signing, interrupted-run recovery and explorer verification, without live writes."""

import json
from types import SimpleNamespace

import pytest
from eth_account import Account

from test_operations import chain  # Reuse the fixed-address mainnet-dependency fixture.
from vyper_grant import deploy
from vyper_grant.core import CONTRACTS, ESCROWS, ROOT


@pytest.fixture
def deployment_run(chain, tmp_path, monkeypatch):
    args = SimpleNamespace(
        account="test", config=tmp_path / "live.json", journal=tmp_path / "deployment.json",
        broadcast=False, verify_only=False, only=None,
    )
    key = chain["tester"].backend.account_keys[0].to_bytes()
    keystore = tmp_path / ".brownie/accounts/test.json"
    keystore.parent.mkdir(parents=True)
    keystore.write_text(json.dumps(Account.encrypt(key, "test-password", kdf="pbkdf2", iterations=1)))
    monkeypatch.setattr(deploy.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(deploy, "rpc", lambda: chain["client"])
    prompts = []
    monkeypatch.setattr(deploy.getpass, "getpass", lambda prompt: prompts.append(prompt) or "test-password")
    monkeypatch.setenv("ETHERSCAN_KEY", "test-key")
    published = []
    monkeypatch.setattr(deploy, "publish", lambda *call: published.append(call))
    return chain, args, keystore, prompts, published


def test_dry_run_then_deploy_verify_and_resume_without_duplicates(deployment_run):
    s, args, keystore, prompts, published = deployment_run
    initial_nonce = s["client"].eth.get_transaction_count(s["deployer"])
    deploy.execute(args)
    assert prompts == published == []
    assert not args.config.exists() and not args.journal.exists()
    assert s["client"].eth.get_transaction_count(s["deployer"]) == initial_nonce
    args.broadcast = True
    deploy.execute(args)
    saved = json.loads(args.config.read_text())
    journal = json.loads(args.journal.read_text())
    assert len(prompts) == 1 and len(published) == 2
    assert saved["deployment_block"] == journal["funding"]["block"]
    assert saved["keeper"] is None  # Deployment does not authorize the signer as keeper.
    for name in ESCROWS:
        assert saved[name] == journal[name]["address"]
        assert s["client"].eth.get_code(saved[name])
    deploy.execute(args)
    assert len(prompts) == 1
    assert s["client"].eth.get_transaction_count(s["deployer"]) == initial_nonce + 2
    keystore.unlink()
    args.verify_only, args.broadcast = True, False
    deploy.execute(args)
    assert len(published) == 6 and len(prompts) == 1


def test_interrupted_broadcast_resumes_recorded_hash(deployment_run, monkeypatch):
    s, args, _, prompts, _ = deployment_run
    args.broadcast = True
    initial_nonce = s["client"].eth.get_transaction_count(s["deployer"])
    send = s["client"].eth.send_raw_transaction

    def uncertain(raw):
        send(raw)
        raise ConnectionError("response lost after inclusion")

    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", uncertain)
    with pytest.raises(ConnectionError):
        deploy.execute(args)
    journal = json.loads(args.journal.read_text())
    first_hash = journal["funding"]["tx"]
    assert "address" not in journal["funding"]
    monkeypatch.setattr(s["client"].eth, "send_raw_transaction", send)
    deploy.execute(args)
    assert json.loads(args.journal.read_text())["funding"]["tx"] == first_hash
    assert s["client"].eth.get_transaction_count(s["deployer"]) == initial_nonce + 2
    assert len(prompts) == 2


@pytest.mark.parametrize("only", ["funding", "proceeds"])
def test_single_deployment_preserves_other_contract(deployment_run, only):
    s, args, _, prompts, published = deployment_run
    other = "proceeds" if only == "funding" else "funding"
    contracts = {"funding": s["funding"], "proceeds": s["escrow"]}
    config = json.loads((ROOT / "config/example.json").read_text())
    config.update({other: contracts[other].address, "deployment_block": 1})
    args.config.write_text(json.dumps(config))
    initial_nonce = s["client"].eth.get_transaction_count(s["deployer"])
    args.only, args.broadcast = only, True
    deploy.execute(args)
    saved = json.loads(args.config.read_text())
    assert saved[other] == contracts[other].address
    assert saved["deployment_block"] == 1
    assert saved[only] != contracts[only].address
    assert len(prompts) == len(published) == 1
    assert published[0][0] == only
    assert s["client"].eth.get_transaction_count(s["deployer"]) == initial_nonce + 1


def test_missing_explorer_key_stops_before_unlocking(deployment_run, monkeypatch):
    s, args, _, prompts, published = deployment_run
    args.broadcast = True
    monkeypatch.delenv("ETHERSCAN_KEY")
    with pytest.raises(ValueError, match="ETHERSCAN_KEY"):
        deploy.execute(args)
    assert prompts == published == []
    assert not args.journal.exists()


def test_changed_build_or_existing_config_prevents_new_deployment(deployment_run):
    _, args, _, prompts, _ = deployment_run
    args.broadcast = True
    args.journal.write_text(json.dumps({"build": "different"}))
    with pytest.raises(ValueError, match="another build"):
        deploy.execute(args)
    args.journal.unlink()
    config = json.loads((ROOT / "config/example.json").read_text())
    config["funding"] = "0x" + "12" * 20
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="unrecorded funding"):
        deploy.execute(args)
    assert prompts == []


def test_explorer_uses_vyper_input_and_waits_for_indexing_and_verification(monkeypatch):
    calls = []
    replies = iter([
        {"status": "0", "result": "Unable to locate ContractCode at address"},
        {"status": "1", "result": "guid"},
        {"status": "0", "result": "Pending in queue"},
        {"status": "1", "result": "Pass - Verified"},
    ])

    def explorer(action, **fields):
        calls.append((action, fields))
        return next(replies)

    monkeypatch.setattr(deploy, "etherscan", explorer)
    monkeypatch.setattr(deploy.time, "sleep", lambda _: None)
    source = deploy.verification_input("proceeds")
    deploy.publish("proceeds", "0x" + "12" * 20, source)
    assert [c[0] for c in calls] == ["verifysourcecode"] * 2 + ["checkverifystatus"] * 2
    fields = calls[0][1]
    assert fields["codeformat"] == "vyper-json"
    assert fields["compilerversion"] == "vyper:0.4.3"
    assert fields["constructorArguments"] == ""
    assert json.loads(fields["sourceCode"]) == source
    assert calls[-1][1] == {"guid": "guid"}


def test_explorer_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr(deploy, "etherscan", lambda *a, **k: {"status": "0", "result": "Bytecode mismatch"})
    with pytest.raises(ValueError, match="Bytecode mismatch"):
        deploy.publish("funding", "0x" + "12" * 20, {})
    monkeypatch.setattr(deploy, "etherscan", lambda *a, **k: {"status": "0", "result": "Contract source code already verified"})
    deploy.publish("funding", "0x" + "12" * 20, {})


def test_confirmed_failed_deployment_is_replaced_only_on_request(deployment_run):
    s, args, _, prompts, _ = deployment_run
    client = s["client"]
    # A creation transaction that reverts: PUSH1 0 PUSH1 0 REVERT.
    failed = client.eth.send_transaction({"from": s["deployer"], "data": "0x60006000fd", "gas": 100_000})
    assert client.eth.get_transaction_receipt(failed).status == 0
    sources = {"funding": deploy.verification_input("funding")}
    fingerprint = deploy.Web3.to_hex(deploy.Web3.keccak(text=json.dumps(sources, sort_keys=True)))
    args.journal.write_text(json.dumps({"build": fingerprint, "funding": {"tx": deploy.Web3.to_hex(failed)}}))
    args.only, args.broadcast = "funding", True
    real_block = client.eth.get_block
    unfinalized = lambda block, *a, **k: {"number": 0} if block == "finalized" else real_block(block, *a, **k)
    deploy.rpc().eth.get_block = unfinalized
    with pytest.raises(ValueError, match="finalized"):
        deploy.execute(args)
    deploy.rpc().eth.get_block = real_block
    with pytest.raises(ValueError, match="retry-failed"):
        deploy.execute(args)
    assert prompts == []
    args.retry_failed = True
    deploy.execute(args)
    journal = json.loads(args.journal.read_text())
    assert journal["funding"]["failed"] == [deploy.Web3.to_hex(failed)]
    assert journal["funding"]["tx"] != journal["funding"]["failed"][0]
    saved = json.loads(args.config.read_text())
    assert saved["funding"] == journal["funding"]["address"]
    assert saved["funding_tx"] == journal["funding"]["tx"]


def test_deploy_errors_print_only_owned_messages():
    leak = ConnectionError("HTTPConnectionPool: Max retries exceeded with url: /v2/SECRETKEY123")
    assert "SECRETKEY123" not in deploy.error_message(leak)
    assert deploy.error_message(leak).startswith("Error: ConnectionError")
    owned = ValueError("Selected deployment addresses are required")
    assert "Selected deployment addresses are required" in deploy.error_message(owned)
    assert "KEY" not in deploy.error_message(ValueError("see https://rpc.example/v2/KEY"))


def test_config_hash_must_match_the_journal(deployment_run):
    s, args, _, prompts, _ = deployment_run
    args.broadcast = True
    deploy.execute(args)
    saved = json.loads(args.config.read_text())
    saved["funding_tx"] = saved["proceeds_tx"]
    args.config.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="differs from the journal"):
        deploy.execute(args)


def test_guard_deploys_with_its_arguments_without_moving_the_report_start(deployment_run):
    s, args, _, prompts, published = deployment_run
    config = json.loads((ROOT / "config/example.json").read_text())
    config.update(proceeds=s["escrow"].address, keeper=s["keeper"], deployment_block=1)
    args.config.write_text(json.dumps(config))
    args.only, args.broadcast = "guard", True
    deploy.execute(args)
    saved = json.loads(args.config.read_text())
    assert saved["guard"] and saved["guard_tx"] and saved["deployment_block"] == 1
    assert published[0][0] == "guard" and published[0][3] == deploy.encoded_arguments(saved, "guard")
    assert len(published[0][3]) == 128


def test_guard_deploy_checks_its_escrow_and_tolerates_no_report_start(deployment_run):
    s, args, _, _, published = deployment_run
    config = json.loads((ROOT / "config/example.json").read_text())
    config.update(proceeds=s["gauge"].address, keeper=s["keeper"], deployment_block=None)
    args.config.write_text(json.dumps(config))
    args.only, args.broadcast = "guard", True
    nonce = s["client"].eth.get_transaction_count(s["deployer"])
    with pytest.raises(ValueError, match="bytecode"):
        deploy.execute(args)
    assert s["client"].eth.get_transaction_count(s["deployer"]) == nonce
    config["proceeds"] = s["escrow"].address
    args.config.write_text(json.dumps(config))
    args.journal.unlink(missing_ok=True)
    deploy.execute(args)
    saved = json.loads(args.config.read_text())
    assert saved["guard"] and saved["guard_tx"] and saved["deployment_block"] is None
