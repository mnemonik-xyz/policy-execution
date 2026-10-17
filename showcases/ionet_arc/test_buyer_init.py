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
        self.home = Path(self.tmp.name) / "buyer"

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

    def test_guardrail_loads_the_written_setup(self):
        run_init("--home", str(self.home))
        cfg = ServerConfig(manifest_path=self.home / "manifest.yaml")
        wallet = (self.home / "buyer.key").read_text().strip()
        self.assertEqual(cfg.buyer_private_key, wallet)
        self.assertEqual(cfg.buyer_account, Account.from_key(wallet).address)
        self.assertEqual(cfg.buyer_acceptance_key_path, self.home / "buyer_acceptance.key")
        self.assertEqual(cfg.escrow_address, buyer_init.MAINNET_BUYER["escrow_address"])

    def test_status_reads_usdc_balance(self):
        run_init("--home", str(self.home))
        with patch.object(buyer_init, "usdc_balance", return_value=buyer_init.Decimal("1.25")) as bal:
            out = run_init("--home", str(self.home), "--status")
        self.assertIn("1.25 USDC", out)
        self.assertEqual(bal.call_args.args[0], buyer_init.MAINNET_BUYER["rpc_url"])


if __name__ == "__main__":
    unittest.main()
