"""Tests for Warrant io.net transcription MCP server."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc.escrow import (
    DEFAULT_CATEGORY,
    encode_journal,
)
from showcases.ionet_arc.server import (
    create_server,
    filter_suitable_gpus,
)


class WarrantTranscriptionServerTests(unittest.IsolatedAsyncioTestCase):
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
        self.mock_escrow.rpc.accounts.return_value = ["0x" + "55" * 20, "0x" + "44" * 20]
        self.mock_escrow.task_id_for.return_value = "0x" + "99" * 32
        self.mock_escrow.get_task.return_value = {
            "task_id": "0x" + "99" * 32,
            "state": "Offered",
            "amount": 1_000_000,
        }
        self.mock_escrow.settle_mock.return_value = {"transactionHash": "0x" + "aa" * 32}

        # Create server with mock EscrowClient and mock_ionet=True
        with patch("showcases.ionet_arc.server.EscrowClient", return_value=self.mock_escrow):
            self.mcp = create_server(self.state_dir, mock_ionet=True)

    async def call(self, tool_name: str, arguments: dict = None) -> dict:
        arguments = arguments or {}
        res = await self.mcp.call_tool(tool_name, arguments)
        return json.loads(res.content[0].text)

    def test_filter_suitable_gpus_sorting_and_constraints(self):
        sample_hardware = [
            # Multi-GPU: should be excluded
            {
                "hardware_id": "gpu_8x_a100",
                "hardware_name": "A100",
                "price": 10.5,
                "available": 4,
                "max_gpus_per_container": 8,
            },
            # Single GPU with 0 available: should be excluded
            {
                "hardware_id": "gpu_1x_empty",
                "hardware_name": "RTX 4090",
                "price": 0.25,
                "available": 0,
                "max_gpus_per_container": 1,
            },
            # Single GPU: RTX 4090 @ $0.30
            {
                "hardware_id": 12,
                "hardware_name": "GeForce RTX 4090",
                "price": 0.30,
                "available": 100,
                "max_gpus_per_container": 1,
            },
            # Single GPU: RTX 3090 @ $0.27 (cheaper)
            {
                "hardware_id": 3,
                "hardware_name": "GeForce RTX 3090",
                "price": 0.27,
                "available": 5,
                "max_gpus_per_container": 1,
            },
            # Single GPU: L40 @ $0.66
            {
                "hardware_id": "gpu_1x_l40",
                "hardware_name": "L40",
                "price": 0.66,
                "available": 10,
                "max_gpus_per_container": 1,
            },
        ]
        filtered = filter_suitable_gpus(sample_hardware)
        self.assertEqual(len(filtered), 3)
        # Verify sorted by price ascending: RTX 3090 (0.27) < RTX 4090 (0.30) < L40 (0.66)
        self.assertEqual(filtered[0]["hardware_id"], 3)
        self.assertEqual(filtered[0]["price_per_hour_usd"], 0.27)
        self.assertEqual(filtered[1]["hardware_id"], 12)
        self.assertEqual(filtered[1]["price_per_hour_usd"], 0.30)
        self.assertEqual(filtered[2]["hardware_id"], "gpu_1x_l40")

    def test_journal_encoding_384_bytes(self):
        journal = encode_journal(
            policy_hash="0x" + "11" * 32,
            chain_id=31337,
            vault="0x" + "22" * 20,
            token="0x" + "33" * 20,
            recipient="0x" + "44" * 20,
            amount=1_000_000,
            task_id="0x" + "55" * 32,
            deliverable_hash="0x" + "66" * 32,
            policy_version=1,
            valid_after=1000,
            valid_until=5000,
            evidence_hash="0x" + "77" * 32,
        )
        self.assertEqual(len(journal), 384)
        self.assertEqual(journal[:32], bytes.fromhex("11" * 32))

    async def test_propose_deployment_generates_valid_terms(self):
        proposal = await self.call("propose_deployment", {"budget_cap_usd": "1.00"})

        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["duration_hours"], 1)
        self.assertEqual(proposal["billing_model"], "duration")
        self.assertEqual(proposal["amount"], 1_000_000)
        self.assertEqual(proposal["category"], DEFAULT_CATEGORY)
        self.assertTrue(proposal["task_id"].startswith("0x"))
        self.assertTrue(proposal["salt"].startswith("0x"))
        self.assertTrue(proposal["policy_hash"].startswith("0x"))
        self.assertIn("TaskEscrow.offer", proposal["instructions"])

    async def test_transcribe_audio_fails_without_prerequisite_deployment(self):
        audio_file = self.state_dir / "test.mp3"
        audio_file.write_bytes(b"dummy audio content")

        with self.assertRaises(ToolError) as ctx:
            await self.call("transcribe_audio", {"audio_path": str(audio_file)})
        self.assertIn("No active deployment session found", str(ctx.exception))

    async def test_deploy_with_escrow_fails_if_unfunded_on_chain(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        # Mock that task is Missing on-chain
        self.mock_escrow.get_task.return_value = {"task_id": task_id, "state": "Missing"}

        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertIn("has not been offered on TaskEscrow", str(ctx.exception))

    async def test_end_to_end_propose_deploy_and_transcribe(self):
        # 1. Propose
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        # 2. Simulate task Offered on-chain with matching terms
        import time
        self.mock_escrow.get_task.return_value = {
            "task_id": task_id,
            "state": "Offered",
            "amount": proposal["amount"],
            "recipient": proposal["recipient"],
            "policy_hash": proposal["policy_hash"],
            "customer": proposal["customer"],
            "settle_by": int(time.time()) + 86400,
        }

        # 3. Deploy with escrow
        dep_result = await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertEqual(dep_result["status"], "deployed")
        self.assertEqual(dep_result["task_id"], task_id)
        self.assertTrue(dep_result["public_url"].startswith("http://127.0.0.1:"))

        # Check acceptance and settlement were called on escrow client
        self.mock_escrow.accept.assert_called_once_with(task_id, sender=proposal["recipient"])
        self.mock_escrow.settle_mock.assert_called_once()
        self.assertEqual(self.mock_escrow.settle_mock.call_args.kwargs.get("sender"), proposal["recipient"])

        # 4. Transcribe audio
        audio_file = self.state_dir / "sample.mp3"
        audio_file.write_bytes(b"mock sample audio bytes")

        trans_res = await self.call("transcribe_audio", {"audio_path": str(audio_file), "language": "en"})
        self.assertEqual(trans_res["status"], "complete")
        self.assertIn("Warrant autonomous transcription pilot", trans_res["text"])
        self.assertEqual(trans_res["language"], "en")

        # 5. Check deployment status
        status = await self.call("get_deployment_status")
        self.assertTrue(status["active"])
        self.assertFalse(status["is_expired"])
        self.assertGreater(status["remaining_seconds"], 3500)

    async def test_deploy_with_escrow_fails_on_terms_mismatch(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]
        import time

        # Amount mismatch
        self.mock_escrow.get_task.return_value = {
            "task_id": task_id,
            "state": "Offered",
            "amount": 999,
            "recipient": proposal["recipient"],
            "policy_hash": proposal["policy_hash"],
            "customer": proposal["customer"],
            "settle_by": int(time.time()) + 86400,
        }
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertIn("does not match proposal", str(ctx.exception))

        # Recipient mismatch
        self.mock_escrow.get_task.return_value["amount"] = proposal["amount"]
        self.mock_escrow.get_task.return_value["recipient"] = "0x" + "99" * 20
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertIn("does not match proposal", str(ctx.exception))

    async def test_deploy_with_escrow_fails_if_settlement_reverts(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]
        import time

        self.mock_escrow.get_task.return_value = {
            "task_id": task_id,
            "state": "Offered",
            "amount": proposal["amount"],
            "recipient": proposal["recipient"],
            "policy_hash": proposal["policy_hash"],
            "customer": proposal["customer"],
            "settle_by": int(time.time()) + 86400,
        }
        self.mock_escrow.settle_mock.side_effect = RuntimeError("execution reverted: InvalidAuthorization")
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertIn("Escrow settlement failed on-chain", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
