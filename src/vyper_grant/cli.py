"""Build unsigned deployments and Safe batches; only the keeper can broadcast."""

import argparse
import json
import sys
from pathlib import Path
from .core import (
    ADDRESSES as A,
    CONTRACTS,
    build,
    address,
    artifact,
    atomic_json,
    contract,
    load_config,
    rpc,
    units,
)
from .chain import historical_vote, report, verify
from .report import render


def batch(sender, *calls, **meta):
    return {
        "version": "1.0",
        "chainId": "1",
        "meta": {"name": "Vyper programme", "safeAddress": sender, **meta},
        "transactions": [
            {
                "to": fn.address,
                "value": "0",
                "data": fn._encode_transaction_data(),
                "contractMethod": None,
                "contractInputsValues": None,
            }
            for fn in calls
        ],
    }


def deployment(client, c, name, deployer):
    if not c.get("payee") or not c.get("start"):
        raise ValueError("Configure payee and start before deployment")
    args = [c["payee"], c["start"], c["weeks"]]
    if name == "proceeds":
        if not c.get("funding"):
            raise ValueError("Deploy and configure funding first")
        args = [c["funding"]]
    compiled = artifact(name)
    factory = client.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx = factory.constructor(*args).build_transaction(
        {"from": address(deployer), "chainId": 1}
    )
    return {
        "contract": CONTRACTS[name],
        "constructor_arguments": args,
        "transaction": tx,
    }


def setup(client, c, allowance):
    _, escrow = verify(client, c)
    m = contract(client, "minter").functions
    calls = [
        contract(client, "token", A["crv"]).functions.approve(escrow.address, allowance)
    ]
    if not m.allowed_to_mint_for(escrow.address, A["receiver"]).call():
        calls.append(m.toggle_approve_mint(escrow.address))
    manager = [escrow.functions.set_keeper(address(c["keeper"]), True)]
    return batch(A["receiver"], *calls), batch(A["manager"], *manager)


def doctor(client, c):
    missing = [
        key
        for key in [
            "funding",
            "proceeds",
            "deployment_block",
            "payee",
            "start",
            "keeper",
        ]
        if c.get(key) is None
    ]
    result = {"missing": missing, "chain_id": client.eth.chain_id}
    if contract(client, "vault").functions.asset().call() != A["usdc"]:
        raise ValueError("Unexpected vault asset")
    if contract(client, "gauge").functions.receiver().call() != A["receiver"]:
        raise ValueError("Unexpected gauge receiver")
    if not missing:
        f, p = verify(client, c)
        result.update(
            funding_assets=str(f.functions.assets().call()),
            remaining_budget=str(f.functions.remaining().call()),
            paused=p.functions.paused().call(),
            keeper_enabled=p.functions.keepers(c["keeper"]).call(),
            mint_enabled=contract(client, "minter")
            .functions.allowed_to_mint_for(p.address, A["receiver"])
            .call(),
            crv_allowance=str(
                contract(client, "token", A["crv"])
                .functions.allowance(A["receiver"], p.address)
                .call()
            ),
        )
    return result


def main():
    p = argparse.ArgumentParser(
        description="Vyper programme: vault funding and crvUSD proceeds"
    )
    p.add_argument("--config", default="config/live.json")
    p.add_argument("--out", default="output")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ["build", "doctor", "quote"]:
        sub.add_parser(name)
    d = sub.add_parser("deploy")
    d.add_argument("contract", choices=CONTRACTS)
    d.add_argument("--deployer", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--allowance-crv", required=True)
    f = sub.add_parser("purchase")
    f.add_argument("--week", type=int, required=True)
    f.add_argument("--usdc", required=True)
    f.add_argument("--reference", help="Human-readable Safe description")
    s = sub.add_parser("set-missing-weight")
    s.add_argument("--week", type=int, required=True)
    r = sub.add_parser("recover")
    r.add_argument("contract", choices=CONTRACTS)
    r.add_argument("--token", required=True)
    r.add_argument("--amount-raw", type=int, required=True)
    k = sub.add_parser("set-keeper")
    k.add_argument("--account", required=True)
    k.add_argument("--disable", action="store_true")
    s = sub.add_parser("set-paused")
    s.add_argument("paused", choices=["true", "false"])
    for name in ["keeper", "checkpoint"]:
        k = sub.add_parser(name)
        k.add_argument("--send", action="store_true")
    r = sub.add_parser("report")
    r.add_argument("--input", help="Render saved data instead of reading the chain")
    args = p.parse_args()
    out = Path(args.out)
    try:
        if args.command == "build":
            for name, compiled in build().items():
                atomic_json(out / (name + ".json"), compiled)
            print("Built both Vyper contracts")
            return
        if args.command == "report" and args.input:
            render(json.loads(Path(args.input).read_text()), out)
            print(str((out / "index.html").resolve()))
            return
        c, client = load_config(args.config), rpc()
        if args.command == "deploy":
            result = deployment(client, c, args.contract, args.deployer)
        elif args.command == "doctor":
            result = doctor(client, c)
        elif args.command == "report":
            render(report(client, c), out)
            print(str((out / "index.html").resolve()))
            return
        elif args.command == "setup":
            vyper, manager = setup(client, c, units(args.allowance_crv))
            atomic_json(out / "setup-vyper.safe.json", vyper)
            atomic_json(out / "setup-manager.safe.json", manager)
            print("Prepared Vyper and manager setup batches")
            return
        elif args.command in ["keeper", "checkpoint"]:
            from .keeper import run

            print(
                json.dumps(
                    run(
                        client,
                        c,
                        args.send,
                        out / "keeper",
                        checkpoint_only=args.command == "checkpoint",
                    ),
                    indent=2,
                )
            )
            return
        else:
            funding, escrow = verify(client, c)
            ef = escrow.functions
            meta = {}
            if args.command == "quote":
                block = client.eth.block_number
                amount = ef.claimable().call(block_identifier=block)
                output = ef.quote(amount).call(block_identifier=block)
                print(
                    json.dumps(
                        {
                            "crv": str(amount),
                            "crvusd": str(output),
                        }
                    )
                )
                return
            if args.command == "purchase":
                fn = funding.functions.purchase(args.week, units(args.usdc, 6))
                if args.reference:
                    meta = {"description": args.reference}
            elif args.command == "set-missing-weight":
                historical = historical_vote(
                    client, args.week, client.eth.get_block("finalized")
                )
                yearn_weight = historical["slope"] * max(
                    historical["end"] - args.week, 0
                )
                fn = ef.set_missing_weight(args.week, yearn_weight)
                meta = {"historicalVote": historical, "yearnWeight": str(yearn_weight)}
            elif args.command == "recover":
                fn = (
                    funding if args.contract == "funding" else escrow
                ).functions.recover(address(args.token), args.amount_raw)
            elif args.command == "set-keeper":
                fn = ef.set_keeper(address(args.account), not args.disable)
            elif args.command == "set-paused":
                fn = ef.set_paused(args.paused == "true")
            fn.call({"from": A["manager"]})
            result = batch(A["manager"], fn, **meta)
        path = out / (args.command + ".json")
        atomic_json(path, result)
        print(str(path))
    except Exception as error:
        # Provider errors may contain authenticated endpoint URLs.
        message = (
            str(error)
            if isinstance(error, (ValueError, FileNotFoundError))
            and "http" not in str(error).lower()
            else type(error).__name__
        )
        print("Error: " + message, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
