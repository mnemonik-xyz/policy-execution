#!/usr/bin/env python3
"""Run the io.net GPU compute MCP server over Streamable HTTP."""
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
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        state_dir=args.state_dir,
        auth_user=args.auth_user,
        auth_pass=args.auth_pass,
    )


if __name__ == "__main__":
    main()
