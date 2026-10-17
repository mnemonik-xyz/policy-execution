"""Tests for the one-command buyer guardrail setup."""

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from eth_account import Account

from showcases.ionet_arc import buyer_init
from showcases.ionet_arc.buyer_mcp_server import ServerConfig


def run_init(*argv: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert buyer_init.main(list(argv)) == 0
    return out.getvalue()


class BuyerInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "buyer"  # macOS: /var is a link to /private/var

    def test_writes_private_keys_and_mainnet_manifest(self):
        out = run_init("--home", str(self.home))

        wallet = self.home / "buyer.key"
        acceptance = self.home / "buyer_acceptance.key"
        for path in (wallet, acceptance):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertRegex(acceptance.read_text().strip(), r"^[0-9a-f]{64}$")

        address = Account.from_key(wallet.read_text().strip()).address
        self.assertIn(address, out)

        manifest = yaml.safe_load((self.home / "manifest.yaml").read_text())
        self.assertEqual(manifest["chain_id"], 5042)
        self.assertEqual(manifest["escrow_address"], buyer_init.MAINNET_BUYER["escrow_address"])
        self.assertEqual(manifest["verifier_address"], buyer_init.MAINNET_BUYER["verifier_address"])
        self.assertEqual(manifest["buyer_address"], address)
        self.assertEqual(manifest["max_budget_usd"], "1.00")

        keys = yaml.safe_load((self.home / "public_keys.yaml").read_text())
        published = yaml.safe_load(buyer_init.MAINNET_PUBLIC_KEYS.read_text())
        self.assertEqual(keys["registry_key"], published["registry_key"])
        self.assertEqual(
            keys["buyer_acceptance_key"],
            buyer_init.acceptance_public_key(acceptance.read_text().strip()),
        )
        self.assertEqual(len(bytes.fromhex(keys["buyer_acceptance_key"][2:])), 33)

    def test_rerun_keeps_the_wallet(self):
        run_init("--home", str(self.home))
        first = (self.home / "buyer.key").read_text()
        first_acc = (self.home / "buyer_acceptance.key").read_text()
        out = run_init("--home", str(self.home), "--max-budget", "0.5")
        self.assertEqual((self.home / "buyer.key").read_text(), first)
        self.assertEqual((self.home / "buyer_acceptance.key").read_text(), first_acc)
        self.assertIn("(existing)", out)
        manifest = yaml.safe_load((self.home / "manifest.yaml").read_text())
        self.assertEqual(manifest["max_budget_usd"], "0.50")

    def test_rejects_budget_above_policy_ceiling(self):
        for bad in ("2.01", "0", "-1", "lots"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                buyer_init.main(["--home", str(self.home), "--max-budget", bad])

    def test_client_config_runs_the_guardrail_with_this_manifest(self):
        run_init("--home", str(self.home))
        config = json.loads((self.home / "mcp.json").read_text())
        guard = config["mcpServers"]["buyer-warrant-guardrail"]
        self.assertIn(str(self.home / "manifest.yaml"), guard["args"])
        self.assertEqual(guard["env"]["PYTHONPATH"], str(buyer_init.REPO_ROOT))
        self.assertIn(buyer_init.SELLER_MCP_URL, config["mcpServers"]["seller-transcription-service"]["args"])
        seller = config["mcpServers"]["seller-transcription-service"]
        self.assertNotIn("env", seller)  # no shared password
        self.assertNotIn("--header", seller["args"])
        self.assertNotIn("SELLER_AUTH", (self.home / "mcp.json").read_text())

    def test_guardrail_loads_the_written_setup(self):
        run_init("--home", str(self.home))
        cfg = ServerConfig(manifest_path=self.home / "manifest.yaml")
        wallet = (self.home / "buyer.key").read_text().strip()
        self.assertEqual(cfg.buyer_private_key, wallet)
        self.assertEqual(cfg.buyer_account, Account.from_key(wallet).address)
        self.assertEqual(cfg.buyer_acceptance_key_path, self.home / "buyer_acceptance.key")
        self.assertEqual(cfg.escrow_address, buyer_init.MAINNET_BUYER["escrow_address"])

    def test_seller_bridge_is_pinned(self):
        run_init("--home", str(self.home))
        seller = json.loads((self.home / "mcp.json").read_text())["mcpServers"]["seller-transcription-service"]
        self.assertEqual(seller["args"][:2], ["-y", f"mcp-remote@{buyer_init.MCP_REMOTE_VERSION}"])
        self.assertRegex(buyer_init.MCP_REMOTE_VERSION, r"^\d+\.\d+\.\d+$")
        out = run_init("--home", str(self.home))
        self.assertIn(f"mcp-remote@{buyer_init.MCP_REMOTE_VERSION}", out)
        self.assertNotIn('"mcp-remote"', out)

    def test_home_and_written_files_are_private(self):
        run_init("--home", str(self.home))
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        for name in ("manifest.yaml", "mcp.json", "public_keys.yaml"):
            self.assertEqual(stat.S_IMODE((self.home / name).stat().st_mode), 0o600, name)

    def test_existing_loose_home_and_keys_are_tightened(self):
        run_init("--home", str(self.home))
        self.home.chmod(0o755)
        for name in ("buyer.key", "buyer_acceptance.key", "manifest.yaml"):
            (self.home / name).chmod(0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_init("--home", str(self.home))
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        for name in ("buyer.key", "buyer_acceptance.key", "manifest.yaml"):
            self.assertEqual(stat.S_IMODE((self.home / name).stat().st_mode), 0o600, name)
        self.assertIn("buyer.key was readable by others", err.getvalue())

    def test_rpc_url_credentials_are_detected_and_warned_about(self):
        for url in (
            "https://user:pass@rpc.example/arc",
            "https://rpc.example/arc?apikey=abc",
            "https://rpc.example/arc?api_key=abc",
            "https://rpc.example/arc?token=abc",
        ):
            self.assertTrue(buyer_init.rpc_url_has_credentials(url), url)
        self.assertFalse(buyer_init.rpc_url_has_credentials("https://rpc.mainnet.arc.io"))

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_init("--home", str(self.home), "--rpc-url", "https://rpc.example/arc?apikey=SECRETVALUE")
        self.assertIn("holds credentials", err.getvalue())
        self.assertNotIn("SECRETVALUE", err.getvalue())
        self.assertEqual(stat.S_IMODE((self.home / "manifest.yaml").stat().st_mode), 0o600)

        quiet = io.StringIO()
        other = Path(self.tmp.name).resolve() / "other"
        with contextlib.redirect_stderr(quiet):
            run_init("--home", str(other), "--rpc-url", "https://rpc.mainnet.arc.io")
        self.assertNotIn("credentials", quiet.getvalue())

    def test_status_reads_usdc_balance(self):
        run_init("--home", str(self.home))
        with patch.object(buyer_init, "usdc_balance", return_value=buyer_init.Decimal("1.25")) as bal:
            out = run_init("--home", str(self.home), "--status")
        self.assertIn("1.25 USDC", out)
        self.assertEqual(bal.call_args.args[0], buyer_init.MAINNET_BUYER["rpc_url"])


if __name__ == "__main__":
    unittest.main()
