"""Tests for Warrant io.net transcription MCP server."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc.escrow import DEFAULT_CATEGORY
from showcases.ionet_arc.seller_mcp_server import (
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
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            self.mcp = create_server(self.state_dir, mock_ionet=True)

    async def call(self, tool_name: str, arguments: dict | None = None) -> dict:
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

    async def test_propose_deployment_generates_valid_terms(self):
        proposal = await self.call("propose_deployment", {"budget_cap_usd": "1.00"})

        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["duration_hours"], 1)
        self.assertEqual(proposal["billing_model"], "duration")
        self.assertEqual(proposal["amount"], 270_000)
        self.assertEqual(proposal["amount_usd"], "0.27")
        self.assertEqual(proposal["category"], DEFAULT_CATEGORY)
        self.assertTrue(proposal["task_id"].startswith("0x"))
        self.assertTrue(proposal["salt"].startswith("0x"))
        self.assertTrue(proposal["policy_hash"].startswith("0x"))
        self.assertIn("TaskEscrow.offer", proposal["instructions"])

    async def test_propose_deployment_calculates_real_hardware_pricing(self):
        # Specific GPU: RTX 4090 ($0.30/hr)
        p4090 = await self.call("propose_deployment", {"hardware_id": 12})
        self.assertEqual(p4090["amount"], 300_000)
        self.assertEqual(p4090["amount_usd"], "0.30")
        self.assertEqual(p4090["duration_hours"], 1)

        # Multi-hour duration: 2 hours on RTX 4090 ($0.60 total)
        p2h = await self.call("propose_deployment", {"hardware_id": 12, "duration_hours": 2})
        self.assertEqual(p2h["amount"], 600_000)
        self.assertEqual(p2h["amount_usd"], "0.60")
        self.assertEqual(p2h["duration_hours"], 2)

    async def test_propose_deployment_enforces_budget_cap(self):
        # Budget cap lower than cheapest available ($0.27)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"budget_cap_usd": "0.20"})
        self.assertIn("exceeds budget cap", str(ctx.exception))

        # Budget cap lower than selected GPU ($0.66 > $0.50)
        with self.assertRaises(ToolError) as ctx:
            await self.call(
                "propose_deployment",
                {"hardware_id": "gpu_1x_l40", "budget_cap_usd": "0.50"},
            )
        self.assertIn("exceeds budget cap", str(ctx.exception))

        # Invalid duration
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"duration_hours": 0})
        self.assertIn("duration_hours must be at least 1", str(ctx.exception))

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

    async def test_deploy_with_escrow_fails_if_container_provisioning_fails(self):
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

        with patch.object(
            self.mcp._tool_manager._tools["deploy_with_escrow"].fn.__self__,
            "_provision_container",
            return_value=None,
        ):
            with self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": task_id})
            self.assertIn("failed to report a ready public URL", str(ctx.exception))
            # Task MUST NOT be accepted on-chain if container provisioning fails (prevents 24h capital lockup)
            self.mock_escrow.accept.assert_not_called()
            # Escrow settlement MUST NOT be called if container is not ready
            self.mock_escrow.settle_mock.assert_not_called()

    async def test_local_server_ssh_deployment_success(self):
        # Create server with local_server=True
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            local_mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                local_server=True,
                ssh_host="petertower",
            )

        async def local_call(tool_name: str, arguments: dict | None = None) -> dict:
            res = await local_mcp.call_tool(tool_name, arguments or {})
            return json.loads(res.content[0].text)

        proposal = await local_call("propose_deployment")
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

        # Mock subprocess.run for ssh docker calls
        mock_run_results = [
            MagicMock(returncode=0),  # rm -f
            MagicMock(returncode=0, stdout="container-id-123\n", stderr=""),  # run -d
        ]
        mock_health_resp = MagicMock(status_code=200)

        with (
            patch("subprocess.run", side_effect=mock_run_results) as mock_subproc,
            patch("httpx2.AsyncClient.get", return_value=mock_health_resp),
        ):
            dep_res = await local_call("deploy_with_escrow", {"task_id": task_id})

            self.assertEqual(dep_res["status"], "deployed")
            self.assertEqual(dep_res["public_url"], "http://petertower:8080")
            self.assertEqual(mock_subproc.call_count, 2)

            # Check first call: docker rm -f
            first_cmd = mock_subproc.call_args_list[0][0][0]
            self.assertEqual(first_cmd[:5], ["ssh", "petertower", "docker", "rm", "-f"])
            self.assertEqual(first_cmd[5], "warrant-transcription")

            # Check second call: docker run
            second_cmd = mock_subproc.call_args_list[1][0][0]
            self.assertEqual(second_cmd[:5], ["ssh", "petertower", "docker", "run", "-d"])
            self.assertIn("--gpus", second_cmd)
            self.assertIn("all", second_cmd)
            self.assertIn("-p", second_cmd)
            self.assertIn("8080:8080", second_cmd)

            # Check teardown logic
            service = local_mcp._tool_manager._tools["deploy_with_escrow"].fn.__self__
            mock_subproc.reset_mock()
            mock_subproc.return_value = MagicMock(returncode=0)
            service._cleanup_ssh_container()
            mock_subproc.assert_called_once()
            teardown_cmd = mock_subproc.call_args[0][0]
            self.assertEqual(teardown_cmd, ["ssh", "petertower", "docker", "stop", "warrant-transcription"])

    async def test_local_server_ssh_deployment_run_failure(self):
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            local_mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                local_server=True,
                ssh_host="petertower",
            )

        async def local_call(tool_name: str, arguments: dict | None = None) -> dict:
            res = await local_mcp.call_tool(tool_name, arguments or {})
            return json.loads(res.content[0].text)

        proposal = await local_call("propose_deployment")
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

        mock_run_results = [
            MagicMock(returncode=0),  # rm -f
            MagicMock(returncode=1, stderr="docker: permission denied"),  # run failure
        ]

        with patch("subprocess.run", side_effect=mock_run_results):
            with self.assertRaises(ToolError) as ctx:
                await local_call("deploy_with_escrow", {"task_id": task_id})
            self.assertIn("Failed to start container on petertower", str(ctx.exception))
            self.assertIn("docker: permission denied", str(ctx.exception))

    async def test_multi_session_concurrency_and_routing(self):
        import time

        self.mock_escrow.task_id_for.side_effect = lambda customer, salt: f"0x{int(salt, 16):064x}"

        # Generate proposal 1
        p1 = await self.call("propose_deployment")
        t1 = p1["task_id"]

        # Generate proposal 2
        p2 = await self.call("propose_deployment")
        t2 = p2["task_id"]
        self.assertNotEqual(t1, t2)

        def mock_get_task(task_id):
            prop = p1 if task_id == t1 else p2
            return {
                "task_id": task_id,
                "state": "Offered",
                "amount": prop["amount"],
                "recipient": prop["recipient"],
                "policy_hash": prop["policy_hash"],
                "customer": prop["customer"],
                "settle_by": int(time.time()) + 86400,
            }

        self.mock_escrow.get_task.side_effect = mock_get_task

        # Deploy both tasks
        res1 = await self.call("deploy_with_escrow", {"task_id": t1})
        self.assertEqual(res1["status"], "deployed")
        res2 = await self.call("deploy_with_escrow", {"task_id": t2})
        self.assertEqual(res2["status"], "deployed")

        # Check status individually by task_id
        st1 = await self.call("get_deployment_status", {"task_id": t1})
        self.assertEqual(st1["task_id"], t1)
        self.assertTrue(st1["active"])

        st2 = await self.call("get_deployment_status", {"task_id": t2})
        self.assertEqual(st2["task_id"], t2)
        self.assertTrue(st2["active"])

        # Check transcription routed specifically to t1 and t2
        audio_file = self.state_dir / "sample.mp3"
        audio_file.write_bytes(b"concurrent mock audio")

        res_trans1 = await self.call("transcribe_audio", {"audio_path": str(audio_file), "task_id": t1})
        self.assertEqual(res_trans1["status"], "complete")

        res_trans2 = await self.call("transcribe_audio", {"audio_path": str(audio_file), "task_id": t2})
        self.assertEqual(res_trans2["status"], "complete")

        # Default without task_id routes to latest active session
        res_default = await self.call("transcribe_audio", {"audio_path": str(audio_file)})
        self.assertEqual(res_default["status"], "complete")

        # Nonexistent task_id raises ToolError
        with self.assertRaises(ToolError) as ctx:
            await self.call("transcribe_audio", {"audio_path": str(audio_file), "task_id": "0xnonexistent"})
        self.assertIn("No deployment session found for task '0xnonexistent'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
