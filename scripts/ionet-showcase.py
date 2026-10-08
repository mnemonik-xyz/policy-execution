#!/usr/bin/env python3
"""io.net/Arc pilot: discover, inspect, deploy once, and request cleanup."""
import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from showcases.ionet_arc.adapter import (PilotError, READ_TOOLS, connected, deploy,  # noqa: E402
                                         destroy, read_json, save)
from showcases.ionet_arc.arc import preflight  # noqa: E402
from showcases.ionet_arc.funding import payment_plan  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--out", required=True, type=pathlib.Path)
    read = sub.add_parser("read")
    read.add_argument("tool", choices=sorted(READ_TOOLS))
    read.add_argument("--args", type=pathlib.Path)
    read.add_argument("--out", required=True, type=pathlib.Path)
    create = sub.add_parser("deploy")
    create.add_argument("--args", required=True, type=pathlib.Path)
    create.add_argument("--state", required=True, type=pathlib.Path)
    create.add_argument("--estimate", required=True, type=pathlib.Path)
    create.add_argument("--usd-pointer", required=True, help="JSON pointer to the provider's total USD price")
    create.add_argument("--max-cost-usd", required=True)
    cleanup = sub.add_parser("destroy")
    cleanup.add_argument("--state", required=True, type=pathlib.Path)
    arc = sub.add_parser("arc-check")
    arc.add_argument("--network", choices=("testnet", "mainnet"), default="testnet")
    arc.add_argument("--rpc")
    arc.add_argument("--out", required=True, type=pathlib.Path)
    funding = sub.add_parser("payment-plan")
    funding.add_argument("--state", required=True, type=pathlib.Path)
    funding.add_argument("--network", choices=("testnet", "mainnet"), required=True)
    funding.add_argument("--solana-wallet", required=True)
    funding.add_argument("--max-payment-usdc", required=True)
    funding.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()

    async def operation(cloud):
        if args.command == "discover":
            return {"inputSchemas": cloud.schemas}
        if args.command == "read":
            arguments = read_json(args.args) if args.args else {}
            result = await cloud.call(args.tool, arguments)
            return {"tool": args.tool, "arguments": arguments,
                    "observed_at": int(time.time()), "result": result}
        if args.command == "deploy":
            return await deploy(cloud, read_json(args.args), args.state,
                                read_json(args.estimate), args.usd_pointer, args.max_cost_usd)
        return await destroy(cloud, args.state)

    if getattr(args, "out", None) and args.out.exists():
        raise PilotError("Output exists; choose a fresh evidence filename")
    if args.command == "arc-check":
        result = preflight(args.network, args.rpc)
    elif args.command == "payment-plan":
        result = payment_plan(read_json(args.state), args.network, args.solana_wallet, args.max_payment_usdc)
    else:
        result = asyncio.run(connected(operation))
    if getattr(args, "out", None):
        save(args.out, result, exclusive=True)
        print(f"Saved {args.out}")
    else:
        print(json.dumps({k: result[k] for k in ("phase", "deployment_id") if k in result}))


def unwrap_error(exc):
    if isinstance(exc, (PilotError, FileExistsError)):
        return exc
    if hasattr(exc, "exceptions"):
        for sub in exc.exceptions:
            found = unwrap_error(sub)
            if found:
                return found
    return None


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        pilot = unwrap_error(exc)
        if pilot:
            print(str(pilot), file=sys.stderr)
            sys.exit(1)
        # Transport/SDK exceptions may contain authorization headers or tool inputs.
        print("Operation failed. If a write was submitted, inspect saved state and io.net before retrying.",
              file=sys.stderr)
        sys.exit(1)
