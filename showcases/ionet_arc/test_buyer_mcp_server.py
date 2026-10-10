"""Tests for Buyer Warrant guardrail MCP server."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc.buyer_mcp_server import create_app, create_server
from showcases.ionet_arc.escrow import DEFAULT_CATEGORY, ensure_policy_hash


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
        expected_hash = ensure_policy_hash(
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            chain_id=31337,
            max_amount=100_000_000,
            state_dir=self.state_dir,
            categories=[DEFAULT_CATEGORY],
        )

        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_hash": expected_hash,
            "policy_version": 1,
            "category": DEFAULT_CATEGORY,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        res = await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertEqual(res["status"], "APPROVED_AND_OFFERED")
        self.assertEqual(res["task_id"], "0x" + "55" * 32)
        self.assertEqual(res["transaction_hash"], "0x" + "02" * 32)
        self.assertEqual(res["customer"], "0x" + "aa" * 20)
        self.assertEqual(res["amount"], 1_000_000)
        self.assertEqual(res["policy_hash"], expected_hash)

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

    async def test_warrant_evaluate_and_offer_policy_hash_mismatch(self):
        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_hash": "0x" + "bad" * 21,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("Policy hash mismatch", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_auto_mint_on_low_balance(self):
        self.mock_escrow.token_balance.return_value = 100  # Insufficient

        expected_hash = ensure_policy_hash(
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            chain_id=31337,
            max_amount=100_000_000,
            state_dir=self.state_dir,
            categories=[DEFAULT_CATEGORY],
        )

        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_hash": expected_hash,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        res = await self.call("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertEqual(res["status"], "APPROVED_AND_OFFERED")
        self.mock_escrow.mint_token.assert_called_once()

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

        expected_hash = ensure_policy_hash(
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            chain_id=31337,
            max_amount=100_000_000,
            state_dir=self.state_dir,
            categories=[DEFAULT_CATEGORY],
        )

        proposal = {
            "task_id": "0x" + "55" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_hash": expected_hash,
            "escrow_address": self.mock_escrow.escrow,
            "token_address": self.mock_escrow.token,
        }

        with self.assertRaises(ToolError) as ctx:
            await server.call_tool("warrant_evaluate_and_offer", {"proposal": proposal})
        self.assertIn("Insufficient token balance", str(ctx.exception))

    async def test_warrant_evaluate_and_offer_task_id_mismatch(self):
        expected_hash = ensure_policy_hash(
            escrow_address=self.mock_escrow.escrow,
            token_address=self.mock_escrow.token,
            chain_id=31337,
            max_amount=100_000_000,
            state_dir=self.state_dir,
            categories=[DEFAULT_CATEGORY],
        )
        proposal = {
            "task_id": "0x" + "99" * 32,
            "amount": 1_000_000,
            "amount_usd": "1.00",
            "duration_hours": 1,
            "salt": "0x" + "66" * 32,
            "recipient": "0x" + "44" * 20,
            "policy_hash": expected_hash,
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


if __name__ == "__main__":
    unittest.main()
