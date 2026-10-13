"""Tests for Buyer Warrant guardrail MCP server."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc.buyer_mcp_server import (
    BuyerServerConfig,
    BuyerWarrantService,
    ServerConfig,
    create_app,
    create_server,
)
from showcases.ionet_arc.escrow import DEFAULT_CATEGORY


class BuyerWarrantServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)

        # Mock EscrowClient
        self.mock_escrow = MagicMock()
        self.mock_escrow.escrow = "0x" + "11" * 20
        self.mock_escrow.token = "0x" + "22" * 20
        self.mock_escrow.verifier = "0x" + "33" * 20
        self.mock_escrow.server_account = "0x" + "44" * 20
        self.mock_escrow.rpc.chain_id.return_value = 31337
        self.mock_escrow.rpc.accounts.return_value = ["0x" + "aa" * 20, "0x" + "bb" * 20]
        self.mock_escrow.token_balance.return_value = 5_000_000
        self.mock_escrow.approve_token.return_value = {"transactionHash": "0x" + "01" * 32}
        self.mock_escrow.offer.return_value = {"transactionHash": "0x" + "02" * 32}
        self.mock_escrow.mint_token.return_value = {"transactionHash": "0x" + "03" * 32}
        self.mock_escrow.task_id_for.return_value = "0x" + "55" * 32
        self.mock_escrow.get_task.return_value = {
            "task_id": "0x" + "99" * 32,
            "state": "Offered",
            "state_idx": 1,
            "customer": "0x" + "aa" * 20,
            "recipient": "0x" + "44" * 20,
            "amount": 1_000_000,
            "policy_hash": "0x" + "88" * 32,
            "policy_version": 1,
            "accept_by": 1700003600,
            "settle_by": 1700086400,
        }

        self.mcp = create_server(
            state_dir=self.state_dir,
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            buyer_account="0x" + "aa" * 20,
            max_budget_usd=2.00,
            max_duration_hours=1,
            allowed_categories=[DEFAULT_CATEGORY],
            auto_mint=True,
            escrow_client=self.mock_escrow,
        )

    async def call(self, tool_name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        res = await self.mcp.call_tool(tool_name, arguments)
        return json.loads(res.content[0].text)

    async def test_warrant_get_policy(self):
        policy = await self.call("warrant_get_policy", {})
        self.assertEqual(policy["buyer_account"], "0x" + "aa" * 20)
        self.assertEqual(policy["max_budget_usd"], 2.00)
        self.assertEqual(policy["max_duration_hours"], 1)
        self.assertEqual(policy["allowed_categories"], [DEFAULT_CATEGORY])
        self.assertEqual(policy["chain_id"], 31337)
        self.assertEqual(policy["escrow_address"], self.mock_escrow.escrow)
        self.assertEqual(policy["token_address"], self.mock_escrow.token)
        self.assertTrue(policy["auto_mint"])

    async def test_warrant_evaluate_and_offer_success(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_version": 1,
            "category": DEFAULT_CATEGORY,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        res = await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertEqual(res["status"], "APPROVED")
        self.assertEqual(res["task_id"], "0x" + "55" * 32)
        self.assertEqual(res["transaction_hash"], "0x" + "02" * 32)
        self.assertEqual(res["customer"], "0x" + "aa" * 20)
        self.assertEqual(res["amount"], 1_000_000)
        self.assertTrue(res["policy_hash"].startswith("0x"))

        self.mock_escrow.approve_token.assert_called_once()
        self.mock_escrow.offer.assert_called_once()

    async def test_warrant_evaluate_and_offer_over_budget(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 2_500_000,
            "amount_usd": "2.50",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("exceeds max budget cap", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_excessive_duration(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 2,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("exceeds max duration", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_invalid_category(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "category": 999,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("not in allowed categories", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_pinned_escrow_mismatch(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": "0x" + "99" * 20,
            "token_address": self.mock_escrow.token,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("does not match pinned escrow", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_without_policy_hash(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        res = await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertEqual(res["status"], "APPROVED")
        self.assertTrue(res["policy_hash"].startswith("0x"))

    async def test_warrant_evaluate_and_offer_auto_mint_on_low_balance(self):
        self.mock_escrow.token_balance.return_value = 100  # Insufficient

        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        res = await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertEqual(res["status"], "APPROVED")
        self.mock_escrow.mint_token.assert_called_once()

    async def test_warrant_sign_acceptance(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "category": DEFAULT_CATEGORY,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        await self.call("warrant_evaluate_and_offer", {"proposal": proposal})

        # Test signing acceptance
        deliv_hash = "0x" + "77" * 32
        res = await self.call(
            "warrant_sign_acceptance",
            {
                "task_id": "0x" + "55" * 32,
                "deliverable_hash": deliv_hash,
                "recipient": "0x" + "44" * 20,
                "amount": 1_000_000,
            },
        )
        self.assertEqual(res["status"], "signed")
        self.assertEqual(res["task_id"], "0x" + "55" * 32)
        self.assertTrue(res["acceptance"]["accepted"])
        self.assertEqual(len(res["signature"]), 64)

    async def test_warrant_evaluate_and_offer_insufficient_balance_no_automint(self):
        server = create_server(
            state_dir=self.state_dir,
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            buyer_account="0x" + "aa" * 20,
            auto_mint=False,
            escrow_client=self.mock_escrow,
        )
        self.mock_escrow.token_balance.return_value = 100

        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        with self.assertRaises(ToolError) as ctx:
            await server.call_tool("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("Insufficient token balance", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_task_id_mismatch(self):
        proposal = {
            "task_id": "0x" + "99" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_version": 1,
            "category": DEFAULT_CATEGORY,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        self.mock_escrow.task_id_for.return_value = "0x" + "55" * 32
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("does not match", str(ctx.exception))

    async def test_warrant_check_escrow_status(self):
        res = await self.call("warrant_check_escrow_status", {"task_id": "0x" + "99" * 32})
        self.assertEqual(res["task_id"], "0x" + "99" * 32)
        self.assertEqual(res["state"], "Offered")
        self.assertEqual(res["amount"], 1_000_000)
        self.mock_escrow.get_task.assert_called_with("0x" + "99" * 32)

    def test_create_app(self):
        app = create_app(
            state_dir=self.state_dir,
            escrow_client=self.mock_escrow,
        )
        self.assertIsNotNone(app)

    def test_create_server_and_app_with_config(self):
        config = ServerConfig(
            state_dir=self.state_dir,
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            buyer_account="0x" + "aa" * 20,
            max_budget_usd=3.50,
            max_duration_hours=2,
            allowed_categories=[DEFAULT_CATEGORY],
            escrow_client=self.mock_escrow,
        )
        self.assertIs(ServerConfig, BuyerServerConfig)
        server = create_server(config)
        self.assertIsNotNone(server)
        app = create_app(config)
        self.assertIsNotNone(app)

    def test_read_initial_parameters_from_manifest(self):
        config = ServerConfig(state_dir=self.state_dir)
        self.assertEqual(config.escrow_address, "0xBeD9B731D9C912541FF083AE41607016BFe93b70")
        self.assertEqual(config.verifier_address, "0xDbefc776e18e34049cAaec736b47122f0583F345")
        self.assertEqual(config.buyer_account, "0x9793Dd93D46F153a2A879165cd163A01b54d8A00")
        self.assertEqual(config.max_budget_usd, 2.00)
        self.assertEqual(config.max_duration_hours, 1)
        self.assertEqual(config.rpc_url, "https://rpc.testnet.arc.io")

    def test_read_initial_parameters_from_custom_manifest_kebab_case(self):
        custom_manifest = Path(self.state_dir) / "custom_manifest.yaml"
        custom_manifest.write_text(yaml.dump({
            "rpc-url": "https://custom.rpc.io",
            "escrow-address": "0x" + "12" * 20,
            "verifier-address": "0x" + "34" * 20,
            "buyer-account": "0x" + "56" * 20,
            "max-budget-usd": 4.50,
            "duration-hours": 3,
        }))
        config = ServerConfig(state_dir=self.state_dir, manifest_path=custom_manifest)
        self.assertEqual(config.rpc_url, "https://custom.rpc.io")
        self.assertEqual(config.escrow_address, "0x" + "12" * 20)
        self.assertEqual(config.verifier_address, "0x" + "34" * 20)
        self.assertEqual(config.buyer_account, "0x" + "56" * 20)
        self.assertEqual(config.max_budget_usd, 4.50)
        self.assertEqual(config.max_duration_hours, 3)

    def test_read_max_budget_derived_from_max_amount(self):
        custom_manifest = Path(self.state_dir) / "custom_manifest_amount.yaml"
        custom_manifest.write_text(yaml.dump({
            "max_amount": 3_500_000,
        }))
        config = ServerConfig(state_dir=self.state_dir, manifest_path=custom_manifest)
        self.assertEqual(config.max_budget_usd, 3.50)

    def test_explicit_parameters_override_manifest(self):
        config = ServerConfig(
            state_dir=self.state_dir,
            rpc_url="https://override.rpc.io",
            escrow_address="0x" + "99" * 20,
            verifier_address="0x" + "88" * 20,
            buyer_account="0x" + "77" * 20,
            max_duration_hours=5,
        )
        self.assertEqual(config.rpc_url, "https://override.rpc.io")
        self.assertEqual(config.escrow_address, "0x" + "99" * 20)
        self.assertEqual(config.verifier_address, "0x" + "88" * 20)
        self.assertEqual(config.buyer_account, "0x" + "77" * 20)
        self.assertEqual(config.max_duration_hours, 5)

    def test_resolve_buyer_account_fails_when_unconfigured(self):
        empty_manifest = Path(self.state_dir) / "empty_manifest.yaml"
        empty_manifest.write_text(yaml.dump({}))
        from showcases.ionet_arc.buyer_mcp_server import BuyerWarrantService

        service = BuyerWarrantService(
            state_dir=self.state_dir,
            escrow_client=self.mock_escrow,
            manifest_path=empty_manifest,
        )
        service.buyer_account = None
        with self.assertRaises(ToolError) as ctx:
            service._resolve_buyer_account()
        self.assertIn("Buyer account is not configured in config/manifest.yaml", str(ctx.exception))

    def test_buyer_private_key_resolved_from_key_file(self):
        key_file = Path(self.state_dir) / "custom_buyer.key"
        key_file.write_text("0x" + "11" * 32 + "\n")
        os.chmod(key_file, 0o600)
        custom_manifest = Path(self.state_dir) / "manifest_with_key.yaml"
        custom_manifest.write_text(yaml.dump({
            "private_key_files": {
                "buyer_key": str(key_file),
            }
        }))
        config = ServerConfig(state_dir=self.state_dir, manifest_path=custom_manifest)
        self.assertEqual(config.buyer_private_key, "0x" + "11" * 32)

    def test_buyer_key_file_shared_with_group_or_others_is_refused(self):
        key_file = Path(self.state_dir) / "shared_buyer.key"
        key_file.write_text("0x" + "11" * 32 + "\n")
        os.chmod(key_file, 0o640)
        with self.assertRaises(PermissionError):
            ServerConfig(state_dir=self.state_dir, buyer_key_path=key_file)

    def test_settle_by_follows_the_compiled_policy_validity(self):
        service = BuyerWarrantService(state_dir=self.state_dir, escrow_client=self.mock_escrow)
        self.mock_escrow.token_balance.return_value = 10_000_000
        self.mock_escrow.offer.return_value = {"transactionHash": "0x" + "cc" * 32}
        deadline = int(time.time()) + 7200
        funded = service._fund_escrow(
            escrow_addr="0x" + "11" * 20, token_addr="0x" + "22" * 20, atomic_amount=1_000_000,
            salt="0x" + "33" * 32, recipient="0x" + "44" * 20, policy_hash="0x" + "55" * 32, policy_version=1,
            buyer_account="0x" + "66" * 20, settle_by=deadline,
        )
        self.assertEqual(funded["settle_by"], deadline)
        self.assertEqual(self.mock_escrow.offer.call_args.kwargs["settle_by"], deadline)
        with self.assertRaises(ToolError):
            service._fund_escrow(
                escrow_addr="0x" + "11" * 20, token_addr="0x" + "22" * 20, atomic_amount=1_000_000,
                salt="0x" + "33" * 32, recipient="0x" + "44" * 20, policy_hash="0x" + "55" * 32, policy_version=1,
                buyer_account="0x" + "66" * 20, settle_by=int(time.time()),
            )

    def test_buyer_private_key_resolved_from_env_var(self):
        with patch.dict(os.environ, {"WARRANT_BUYER_KEY": "0x" + "22" * 32}):
            config = ServerConfig(state_dir=self.state_dir, buyer_key_path=Path("/nonexistent"))
            self.assertEqual(config.buyer_private_key, "0x" + "22" * 32)


if __name__ == "__main__":
    unittest.main()
