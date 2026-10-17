"""Set up the buyer guardrail for the Arc mainnet transcription seller with one command.

The command writes everything the guardrail needs into one private directory:

- a new wallet key (a burner wallet: fund it with a small amount of USDC only),
- a new acceptance key, which signs that the GPU worker answered,
- a public keys file with the registry key and the acceptance public key,
- a manifest with the mainnet escrow, verifier and token addresses.

It then prints the MCP client configuration for Claude Desktop, Claude Code and Cursor.
The keys never leave this machine. A second run reuses the existing keys, so it never
replaces a wallet that holds money.

    uvx warrant-buyer init            # set up, or print the setup again
    uvx warrant-buyer status          # show the wallet's USDC balance

From a clone of the repository: python -m showcases.ionet_arc.buyer_init [--status].
"""

import argparse
import json
import os
import pathlib
import secrets
import shutil
import sys
from decimal import Decimal, InvalidOperation

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if __package__ != "warrant_buyer":  # a repository checkout, not the installed package
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_keys import keys  # noqa: E402

from .arc import NETWORKS, USDC  # noqa: E402
from .escrow import SHOWCASE_DIR, EthRpc, find_warrant_binary  # noqa: E402

# Installed from PyPI as warrant-buyer, or run from a clone of the repository.
PACKAGED = __package__ == "warrant_buyer"

DEFAULT_HOME = pathlib.Path.home() / ".warrant-buyer"
MAINNET_PUBLIC_KEYS = SHOWCASE_DIR / "config" / "mainnet-public_keys.yaml"
SELLER_MCP_URL = "https://seller.mnemonik.xyz/mcp"
EXPLORER = "https://explorer.arc.io"

# The values that the seller MCP on Arc mainnet uses now. See README.md, "Buyer setup".
MAINNET_BUYER = {
    "chain_id": NETWORKS["mainnet"]["chain_id"],
    "rpc_url": NETWORKS["mainnet"]["rpc"],
    "escrow_address": "0x5fae10ac58bf90c17f516292883a4f17c0768002",
    "verifier_address": "0xdfe2fdb1d9942af02f99facfaa5b22d0031ad76c",
    "token_address": USDC,
    "category": 42,
    "max_budget_usd": "1.00",
    "duration_hours": 1,
    "validity_seconds": 900,
}
MAX_BUDGET_CEILING = Decimal("2.00")  # amount_at_most in config/buyer_policy_template.yaml


def write_secret(path: pathlib.Path, text: str) -> None:
    """Create a file that only the owner can read. Never overwrite an existing one."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def read_hex_key(path: pathlib.Path) -> str:
    raw = path.read_text().strip().lower()
    return raw[2:] if raw.startswith("0x") else raw


def ensure_wallet_key(home: pathlib.Path) -> tuple[pathlib.Path, str, bool]:
    path = home / "buyer.key"
    created = not path.exists()
    if created:
        write_secret(path, "0x" + Account.create().key.hex().removeprefix("0x") + "\n")
    address = Account.from_key(bytes.fromhex(read_hex_key(path))).address
    return path, address, created


def ensure_acceptance_key(home: pathlib.Path) -> tuple[pathlib.Path, str]:
    """Same form as init.py and `openssl rand -hex 32`: 64 hex characters without 0x."""
    path = home / "buyer_acceptance.key"
    if not path.exists():
        write_secret(path, secrets.token_hex(32) + "\n")
    return path, acceptance_public_key(read_hex_key(path))


def acceptance_public_key(private_hex: str) -> str:
    """Compressed secp256k1 public key, the form that `warrant-evidence public-key` prints."""
    return "0x" + keys.PrivateKey(bytes.fromhex(private_hex)).public_key.to_compressed_bytes().hex()


def parse_budget(raw: str) -> str:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise SystemExit(f"--max-budget must be a number of USD, not {raw!r}") from exc
    if not Decimal(0) < value <= MAX_BUDGET_CEILING:
        raise SystemExit(f"--max-budget must be above 0 and at most {MAX_BUDGET_CEILING} USD")
    return f"{value:.2f}"


def write_config(
    home: pathlib.Path,
    buyer_address: str,
    wallet_key: pathlib.Path,
    acceptance_key: pathlib.Path,
    acceptance_pub: str,
    max_budget_usd: str,
    rpc_url: str | None,
) -> pathlib.Path:
    """Write the public keys file and the manifest. Both hold no secret, so a rerun rewrites them."""
    registry_key = yaml.safe_load(MAINNET_PUBLIC_KEYS.read_text())["registry_key"]
    public_keys = home / "public_keys.yaml"
    public_keys.write_text(
        yaml.safe_dump({"registry_key": registry_key, "buyer_acceptance_key": acceptance_pub}, sort_keys=False)
    )

    manifest = dict(MAINNET_BUYER)
    manifest["max_budget_usd"] = max_budget_usd
    if rpc_url:
        manifest["rpc_url"] = rpc_url
    manifest["buyer_address"] = buyer_address
    manifest["public_files"] = {"public_keys": str(public_keys)}
    manifest["private_key_files"] = {
        "buyer_key": str(wallet_key),
        "buyer_acceptance_key": str(acceptance_key),
    }
    manifest_path = home / "manifest.yaml"
    manifest_path.write_text(
        "# Buyer guardrail manifest for Arc mainnet, written by buyer_init.py.\n"
        + yaml.safe_dump(manifest, sort_keys=False)
    )
    return manifest_path


def package_version() -> str:
    from importlib.metadata import version

    return version("warrant-buyer")


def guardrail_server(home: pathlib.Path, manifest: pathlib.Path) -> dict:
    run_args = ["--transport", "stdio", "--manifest", str(manifest), "--state-dir", str(home / "state")]
    if PACKAGED:
        # Pin the version: a later release must not change the guardrail under a funded wallet unasked.
        # Desktop clients start with a short PATH, so give uvx by its full path.
        uvx = shutil.which("uvx") or "uvx"
        return {"command": uvx, "args": [f"warrant-buyer=={package_version()}", "guardrail", *run_args]}
    return {
        "command": sys.executable,
        "args": ["-m", "showcases.ionet_arc.buyer_mcp_server", *run_args],
        "env": {"PYTHONPATH": str(REPO_ROOT)},
    }


def seller_server() -> dict:
    return {
        "command": "npx",
        "args": ["-y", "mcp-remote", SELLER_MCP_URL, "--header", "Authorization:${SELLER_AUTH}"],
        "env": {"SELLER_AUTH": "Basic <base64 of user:password>"},
    }


def client_config(home: pathlib.Path, manifest: pathlib.Path) -> dict:
    """The mcpServers block for Claude Desktop and Cursor (mcp.json)."""
    return {
        "mcpServers": {
            "buyer-warrant-guardrail": guardrail_server(home, manifest),
            "seller-transcription-service": seller_server(),
        }
    }


def claude_code_commands(home: pathlib.Path, manifest: pathlib.Path) -> list[str]:
    guard = guardrail_server(home, manifest)
    args = " ".join(json.dumps(a) for a in guard["args"])
    env = "".join(f" --env {k}={json.dumps(v)}" for k, v in guard.get("env", {}).items())
    return [
        f"claude mcp add buyer-warrant-guardrail{env} -- {json.dumps(guard['command'])} {args}",
        f"claude mcp add --transport http seller-transcription-service {SELLER_MCP_URL} "
        '--header "Authorization: Basic <base64 of user:password>"',
    ]


STATUS_COMMAND = "uvx warrant-buyer status" if PACKAGED else "python -m showcases.ionet_arc.buyer_init --status"
INIT_COMMAND = "uvx warrant-buyer init" if PACKAGED else "python -m showcases.ionet_arc.buyer_init"


def missing_binaries() -> list[str]:
    missing = []
    for name in ("warrant-policy", "warrant-evidence"):
        try:
            find_warrant_binary(name)
        except FileNotFoundError:
            missing.append(name)
    return missing


def usdc_balance(rpc_url: str, address: str) -> Decimal:
    data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")  # balanceOf(address)
    raw = EthRpc(rpc_url).rpc("eth_call", [{"to": USDC, "data": data}, "latest"])
    return Decimal(int(raw, 16)) / Decimal(1_000_000)


def print_setup(home, address, created, manifest, max_budget_usd) -> None:
    config_file = home / "mcp.json"
    print()
    print("Warrant buyer guardrail is ready.")
    print(f"  Directory: {home}")
    print(f"  Wallet:    {address}  ({'new' if created else 'existing'})")
    print(f"  Cap:       {max_budget_usd} USD per deal")
    print()
    print("1. Fund the wallet")
    print(f"   Send about 1.10 USDC to {address} on Arc mainnet (chain ID 5042).")
    print("   USDC also pays the gas on Arc. Send only what you are ready to spend.")
    print(f"   Explorer: {EXPLORER}/address/{address}")
    print(f"   Check:    {STATUS_COMMAND}")
    print()
    print("2. Connect your MCP client")
    print("   Claude Desktop or Cursor: merge this into claude_desktop_config.json or mcp.json.")
    print(f"   The same block is in {config_file}.")
    print("   Put your seller credential into SELLER_AUTH. Ask the operator for it.")
    print()
    print(json.dumps(client_config(home, manifest), indent=2))
    print()
    print("   Claude Code:")
    for cmd in claude_code_commands(home, manifest):
        print(f"   {cmd}")
    print()
    print('3. Restart the client and ask: "Transcribe audio.mp3."')
    missing = missing_binaries()
    if missing:
        print()
        print(f"Before the first purchase, build {', '.join(missing)} in {REPO_ROOT}:")
        print("   cargo build --release -p warrant-host")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Set up the Warrant buyer guardrail for the Arc mainnet transcription seller."
    )
    parser.add_argument(
        "--home", type=pathlib.Path, default=DEFAULT_HOME, help=f"Setup directory (default: {DEFAULT_HOME})"
    )
    parser.add_argument(
        "--max-budget", default=MAINNET_BUYER["max_budget_usd"], help="Cap per deal in USD (default: 1.00)"
    )
    parser.add_argument("--rpc-url", default=None, help="Your own Arc mainnet RPC (default: the public one)")
    parser.add_argument("--status", action="store_true", help="Show the wallet address and its USDC balance, then exit")
    args = parser.parse_args(argv)

    home = args.home.expanduser().resolve()

    if args.status:
        wallet = home / "buyer.key"
        if not wallet.exists():
            print(f"No wallet in {home}. Run: {INIT_COMMAND}", file=sys.stderr)
            return 1
        address = Account.from_key(bytes.fromhex(read_hex_key(wallet))).address
        manifest = yaml.safe_load((home / "manifest.yaml").read_text()) if (home / "manifest.yaml").exists() else {}
        rpc_url = args.rpc_url or manifest.get("rpc_url") or MAINNET_BUYER["rpc_url"]
        print(f"Wallet:  {address}")
        print(f"Balance: {usdc_balance(rpc_url, address)} USDC on chain {MAINNET_BUYER['chain_id']}")
        return 0

    max_budget_usd = parse_budget(args.max_budget)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    wallet_key, address, created = ensure_wallet_key(home)
    acceptance_key, acceptance_pub = ensure_acceptance_key(home)
    manifest = write_config(home, address, wallet_key, acceptance_key, acceptance_pub, max_budget_usd, args.rpc_url)
    (home / "mcp.json").write_text(json.dumps(client_config(home, manifest), indent=2) + "\n")
    print_setup(home, address, created, manifest, max_budget_usd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
