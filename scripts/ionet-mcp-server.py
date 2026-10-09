#!/usr/bin/env python3
"""Run the Warrant GPU transcription MCP server over Streamable HTTP."""
import argparse
import os
import pathlib
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from showcases.ionet_arc.server import run_server  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "localhost"),
        help="Host address to bind (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port number to bind (default: 8000)",
    )
    parser.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("IONET_STATE_DIR", "artifacts/state")),
        help="Directory to persist state journals (default: artifacts/state)",
    )
    parser.add_argument(
        "--auth-user",
        default=os.environ.get("MCP_AUTH_USER"),
        help="HTTP Basic Auth username (or set MCP_AUTH_USER)",
    )
    parser.add_argument(
        "--auth-pass",
        default=os.environ.get("MCP_AUTH_PASS"),
        help="HTTP Basic Auth password (or set MCP_AUTH_PASS)",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("WARRANT_RPC_URL", "http://127.0.0.1:8545"),
        help="Ethereum / Arc JSON-RPC URL (default: http://127.0.0.1:8545)",
    )
    parser.add_argument(
        "--escrow-address",
        default=os.environ.get("WARRANT_ESCROW"),
        help="TaskEscrow contract address",
    )
    parser.add_argument(
        "--token-address",
        default=os.environ.get("WARRANT_TOKEN"),
        help="Payment token contract address (e.g. USDC)",
    )
    parser.add_argument(
        "--verifier-address",
        default=os.environ.get("WARRANT_VERIFIER"),
        help="zkVM / Journal verifier contract address",
    )
    parser.add_argument(
        "--server-account",
        default=os.environ.get("WARRANT_SERVER_ACCOUNT"),
        help="Server recipient / agent account address",
    )
    parser.add_argument(
        "--mock-ionet",
        action="store_true",
        default=os.environ.get("MOCK_IONET", "").lower() in ("1", "true"),
        help="Run using local mock worker instead of live io.net CaaS",
    )
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        state_dir=args.state_dir,
        auth_user=args.auth_user,
        auth_pass=args.auth_pass,
        rpc_url=args.rpc_url,
        escrow_address=args.escrow_address,
        token_address=args.token_address,
        verifier_address=args.verifier_address,
        server_account=args.server_account,
        mock_ionet=args.mock_ionet,
    )


if __name__ == "__main__":
    main()
