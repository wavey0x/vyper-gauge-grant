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
    printable,
    rpc,
    units,
)
from .chain import enabled_keepers, historical_vote, report, serialize, verify

PROCEEDS_GAS = 1_000_000  # the constructor may pay the controller's weekly update (~892k)
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
    compiled = artifact(name)
    factory = client.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx = factory.constructor().build_transaction(
        {"from": address(deployer), "chainId": 1}
    )
    # The single gas rule for signed, unsigned and cost-estimate paths.
    tx["gas"] = tx["gas"] * 12 // 10 + (PROCEEDS_GAS if name == "proceeds" else 0)
    return {
        "contract": CONTRACTS[name],
        "constructor_arguments": [],
        "transaction": tx,
    }


def allowance_calls(client, spender, amount):
    """CRV only changes an allowance to or from zero, so always reset first; amount is the new total."""
    crv = contract(client, "token", A["crv"]).functions
    return [crv.approve(spender, 0)] + ([crv.approve(spender, amount)] if amount else [])


def setup(client, c, allowance, replace=None, replace_block=None):
    (escrow,) = verify(client, c, names=("proceeds",))
    m = contract(client, "minter").functions
    block = client.eth.block_number
    vyper, manager = [], []
    if replace:
        old_address = address(replace)
        if old_address == escrow.address:
            raise ValueError("The escrow being replaced must differ from the configured one")
        if replace_block is None:
            raise ValueError("--replace-block, the old escrow's creation block, is required")
        # Only the current ProceedsEscrow build is handled; revoke older sellers manually.
        (old,) = verify(client, {"proceeds": old_address}, names=("proceeds",))
        old_mint = m.allowed_to_mint_for(old.address, A["receiver"]).call(block_identifier=block)
        if not old.functions.paused().call(block_identifier=block):
            manager.append(old.functions.set_paused(True))
        for account in enabled_keepers(client, old, replace_block):
            manager.append(old.functions.set_keeper(account, False))
        vyper += allowance_calls(client, old.address, 0)
        if old_mint:
            vyper.append(m.toggle_approve_mint(old.address))
    vyper += allowance_calls(client, escrow.address, allowance)
    # toggle_approve_mint flips the flag, so it is built from this read and must run once.
    mint = m.allowed_to_mint_for(escrow.address, A["receiver"]).call(block_identifier=block)
    if not mint:
        vyper.append(m.toggle_approve_mint(escrow.address))
    manager.append(escrow.functions.set_keeper(address(c["keeper"]), True))
    meta = {"readBlock": block, "mintEnabledAtRead": mint}
    if replace:
        meta.update(replacing=old.address, oldMintEnabledAtRead=old_mint)
    return batch(A["receiver"], *vyper, **meta), batch(A["manager"], *manager, readBlock=block)


def doctor(client, c):
    missing = [
        key
        for key in [
            "funding",
            "proceeds",
            "deployment_block",
            "keeper",
        ]
        if c.get(key) is None
    ]
    result = {"missing": missing, "chain_id": client.eth.chain_id}
    if contract(client, "vault").functions.asset().call() != A["usdc"]:
        raise ValueError("Unexpected vault asset")
    if contract(client, "gauge").functions.receiver().call() != A["receiver"]:
        raise ValueError("Unexpected gauge receiver")
    verified = []
    if c.get("funding"):
        (f,) = verify(client, c, names=("funding",))
        verified.append("funding")
        result.update(
            funding_assets=str(f.functions.assets().call()),
            funding_recipient=f.functions.recipient().call(),
            funding_next_week=f.functions.next_week().call(),
            funding_can_work=f.functions.canWork().call(),
        )
    if c.get("proceeds"):
        (p,) = verify(client, c, names=("proceeds",))
        verified.append("proceeds")
        result.update(
            paused=p.functions.paused().call(),
            cooldown=p.functions.cooldown().call(),
            can_work=p.functions.canWork().call(),
            yearn_receiver=p.functions.yearn_receiver().call(),
            keeper_enabled=bool(c.get("keeper")) and p.functions.keepers(c["keeper"]).call(),
            mint_enabled=contract(client, "minter")
            .functions.allowed_to_mint_for(p.address, A["receiver"])
            .call(),
            crv_allowance=str(
                contract(client, "token", A["crv"])
                .functions.allowance(A["receiver"], p.address)
                .call()
            ),
        )
        if c.get("operating"):
            result["problems"] = [
                text
                for failed, text in [
                    (not result["mint_enabled"], "Vyper's mint permission is off"),
                    (result["crv_allowance"] == "0", "Vyper's CRV allowance is zero"),
                    (not result["keeper_enabled"], "The keeper is not enabled"),
                ]
                if failed
            ]
    # Only contracts verify() actually checked are labelled.
    result["creation_check"] = {
        n: "creation transaction" if c.get(n + "_tx") else "runtime only" for n in verified
    }
    return result


def main():
    p = argparse.ArgumentParser(
        description="Vyper programme: vault funding and USDC proceeds"
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
    s.add_argument("--replace", help="Previous ProceedsEscrow to pause and revoke")
    s.add_argument("--replace-block", type=int, help="Creation block of the previous escrow")
    s = sub.add_parser("allowance", help="Vyper batch that only resets the CRV allowance")
    s.add_argument("--crv", required=True, help="New total allowance in CRV")
    f = sub.add_parser("fund-round")
    f.add_argument("--usdc", required=True)
    f.add_argument("--reference", help="Human-readable Safe description")
    f = sub.add_parser("cancel-round")
    f.add_argument("--governance", action="store_true", help="Cancel funding as ychad.eth")
    s = sub.add_parser("set-missing-weight")
    s.add_argument("--week", type=int, required=True)
    r = sub.add_parser("recover")
    r.add_argument("contract", choices=["funding"])
    r.add_argument("--token", required=True)
    r.add_argument("--amount-raw", type=int, required=True)
    r.add_argument(
        "--governance", action="store_true", help="Recover funding to ychad.eth"
    )
    r = sub.add_parser("withdraw", help="Send held proceeds or another token to a recipient")
    r.add_argument("--token", required=True)
    r.add_argument("--amount-raw", type=int, required=True)
    r.add_argument("--recipient", required=True)
    r.add_argument("--governance", action="store_true", help="Withdraw proceeds as ychad.eth")
    r = sub.add_parser("set-yearn-receiver")
    r.add_argument(
        "--account",
        required=True,
        help="Use the proceeds contract to resume accumulation",
    )
    r = sub.add_parser("set-recipient")
    r.add_argument("--account", required=True)
    k = sub.add_parser("set-keeper")
    k.add_argument("--account", required=True)
    k.add_argument("--disable", action="store_true")
    s = sub.add_parser("set-cooldown")
    s.add_argument("--seconds", type=int, required=True)
    s = sub.add_parser("set-paused")
    s.add_argument("paused", choices=["true", "false"])
    for name in ["keeper", "checkpoint", "funding-keeper"]:
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
            vyper, manager = setup(
                client, c, units(args.allowance_crv), args.replace, args.replace_block
            )
            atomic_json(out / "setup-vyper.safe.json", vyper)
            atomic_json(out / "setup-manager.safe.json", manager)
            print("Prepared Vyper and manager setup batches")
            return
        elif args.command in ["keeper", "checkpoint", "funding-keeper"]:
            from .keeper import FAILURES, run

            result = run(
                client,
                c,
                args.send,
                out / "keeper",
                checkpoint_only=args.command == "checkpoint",
                funding_only=args.command == "funding-keeper",
            )
            print(json.dumps(result, indent=2))
            if result["status"] in FAILURES:
                # Monitoring distinguishes a failed simulation from an ordinary skip.
                raise SystemExit(3)
            return
        else:
            funding_only = args.command in ["fund-round", "cancel-round", "set-recipient", "recover"]
            if funding_only:
                (funding,) = verify(client, c, names=("funding",))
            else:
                (escrow,) = verify(client, c, names=("proceeds",))
                ef = escrow.functions
            meta = {}
            sender = A["manager"]
            if args.command == "allowance":
                block = client.eth.block_number
                current = contract(client, "token", A["crv"]).functions.allowance(
                    A["receiver"], escrow.address
                ).call(block_identifier=block)
                result = batch(
                    A["receiver"],
                    *allowance_calls(client, escrow.address, units(args.crv)),
                    readBlock=block,
                    allowanceAtRead=str(current),
                )
                path = out / "allowance-vyper.safe.json"
                atomic_json(path, result)
                print(str(path))
                return
            if args.command == "quote":
                from .keeper import quote_work

                block = client.eth.block_number
                print(json.dumps(serialize(quote_work(escrow, c, block))))
                return
            if args.command == "fund-round":
                fn = funding.functions.fund_round(units(args.usdc, 6))
                if args.reference:
                    meta = {"description": args.reference}
            elif args.command == "cancel-round":
                if args.governance:
                    sender = A["governance"]
                fn = funding.functions.cancel()
            elif args.command == "set-missing-weight":
                if args.week < ef.DEPLOYED_WEEK().call():
                    raise ValueError("The week predates this escrow")
                if ef.weeks(args.week).call()[0]:
                    raise ValueError("The week already has a snapshot")
                try:
                    historical = historical_vote(
                        client,
                        args.week,
                        client.eth.get_block("finalized"),
                        c.get("deployment_block"),
                    )
                except ValueError:
                    raise
                except Exception:
                    raise ValueError(
                        "The RPC cannot serve historical state for this week; use an archive node"
                    ) from None
                yearn_weight = historical["slope"] * max(
                    historical["end"] - args.week, 0
                )
                fn = ef.set_missing_weight(args.week, yearn_weight)
                meta = {"historicalVote": historical, "yearnWeight": str(yearn_weight)}
            elif args.command == "recover":
                if args.governance:
                    sender = A["governance"]
                fn = funding.functions.recover(address(args.token), args.amount_raw)
            elif args.command == "withdraw":
                if address(args.token) == A["crv"]:
                    if args.governance:
                        raise ValueError("Only the Vyper multisig can withdraw CRV")
                    sender = A["receiver"]
                elif args.governance:
                    sender = A["governance"]
                fn = ef.withdraw(
                    address(args.token), args.amount_raw, address(args.recipient)
                )
            elif args.command == "set-yearn-receiver":
                fn = ef.set_yearn_receiver(address(args.account))
            elif args.command == "set-recipient":
                fn = funding.functions.set_recipient(address(args.account))
            elif args.command == "set-keeper":
                fn = ef.set_keeper(address(args.account), not args.disable)
            elif args.command == "set-cooldown":
                if args.seconds < 600:
                    raise ValueError("Cooldown must be at least 600 seconds")
                fn = ef.set_cooldown(args.seconds)
            elif args.command == "set-paused":
                fn = ef.set_paused(args.paused == "true")
            fn.call({"from": sender})
            result = batch(sender, fn, **meta)
        path = out / (args.command + ".json")
        atomic_json(path, result)
        print(str(path))
    except Exception as error:
        print("Error: " + printable(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
