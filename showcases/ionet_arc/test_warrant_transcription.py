"""Tests for Warrant io.net transcription MCP server."""

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc.escrow import DEFAULT_CATEGORY, compile_buyer_policy
from showcases.ionet_arc.seller_mcp_server import (
    SellerServerConfig,
    ServerConfig,
    create_app,
    create_server,
    filter_suitable_gpus,
)


def encode_audio(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


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

        # Create valid stored test credential and signature matching the mock escrow scope
        import yaml

        from showcases.ionet_arc.escrow import address20, sign_vendor_credential

        cred_obj = {
            "scope": {
                "chain_id": 31337,
                "vault": list(address20(self.mock_escrow.escrow)),
                "token": list(address20(self.mock_escrow.token)),
            },
            "recipient": list(address20(self.mock_escrow.server_account)),
            "category": DEFAULT_CATEGORY,
            "valid_after": int(time.time()) - 3600,
            "valid_until": int(time.time()) + 86400,
        }
        sig_bytes = sign_vendor_credential(cred_obj)
        self.cred_path = self.state_dir / "vendor_credential.yaml"
        self.sig_path = self.state_dir / "vendor_signature.yaml"
        self.cred_path.write_text(yaml.dump({
            "scope": {
                "chain_id": 31337,
                "vault": self.mock_escrow.escrow,
                "token": self.mock_escrow.token,
            },
            "recipient": self.mock_escrow.server_account,
            "category": DEFAULT_CATEGORY,
            "valid_after": cred_obj["valid_after"],
            "valid_until": cred_obj["valid_until"],
        }))
        self.sig_path.write_text(yaml.dump({"signature": "0x" + bytes(sig_bytes).hex()}))

        # Create server with mock EscrowClient, mock_ionet=True, and stored credential paths
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            self.mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                vendor_credential_path=self.cred_path,
                vendor_signature_path=self.sig_path,
            )

    async def call(self, tool_name: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        res = await self.mcp.call_tool(tool_name, arguments)
        return json.loads(res.content[0].text)

    def _create_mock_offered_task(self, proposal: dict) -> tuple[dict, dict]:
        buyer_policy, policy_hash = compile_buyer_policy(
            chain_id=proposal["chain_id"],
            escrow_address=proposal["escrow_address"],
            token_address=proposal["token_address"],
            max_amount=proposal["amount"],
        )
        task_data = {
            "task_id": proposal["task_id"],
            "state": "Offered",
            "amount": proposal["amount"],
            "recipient": proposal["recipient"],
            "policy_hash": policy_hash,
            "customer": proposal["customer"],
            "settle_by": int(time.time()) + 86400,
        }
        return buyer_policy, task_data

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
        self.assertNotIn("policy_hash", proposal)
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
            await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file)})
        self.assertIn("No active deployment session found", str(ctx.exception))

    async def test_deploy_with_escrow_fails_if_unfunded_on_chain(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        # Mock that task is Missing on-chain
        self.mock_escrow.get_task.return_value = {"task_id": task_id, "state": "Missing"}

        buyer_policy, _ = self._create_mock_offered_task(proposal)

        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        self.assertIn("has not been offered on TaskEscrow", str(ctx.exception))

    async def test_end_to_end_propose_deploy_and_transcribe(self):
        # 1. Propose
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        # 2. Simulate task Offered on-chain with matching terms
        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data

        # 3. Deploy with escrow
        dep_result = await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        self.assertIn(dep_result["status"], ("provisioning", "ready"))
        self.assertEqual(dep_result["task_id"], task_id)

        # Check acceptance was called on escrow client
        self.mock_escrow.accept.assert_called_once_with(task_id, sender=proposal["recipient"])

        # 4. Check deployment status
        status = await self.call("get_deployment_status", {"task_id": task_id})
        self.assertEqual(status["status"], "ready")
        self.assertTrue(status["active"])
        self.assertFalse(status["is_expired"])
        self.assertTrue(status["public_url"].startswith("http://127.0.0.1:"))
        self.assertGreater(status["remaining_seconds"], 3500)

        # 5. Submit acceptance and settle escrow
        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)
        settle_res = await self.call(
            "submit_acceptance",
            {"task_id": task_id, "acceptance": acceptance, "signature": sig},
        )
        self.assertEqual(settle_res["status"], "settled")
        self.mock_escrow.settle_mock.assert_called_once()

        # 6. Transcribe audio
        audio_file = self.state_dir / "sample.mp3"
        audio_file.write_bytes(b"mock sample audio bytes")

        trans_res = await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file), "language": "en"})
        self.assertEqual(trans_res["status"], "complete")
        self.assertIn("Warrant autonomous transcription pilot", trans_res["text"])
        self.assertEqual(trans_res["language"], "en")

        with self.assertRaises(ToolError) as ctx:
            await self.call("transcribe_audio", {"audio_base64": "/etc/passwd"})
        self.assertIn("not valid base64", str(ctx.exception))

    async def test_deploy_with_escrow_fails_on_terms_mismatch(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)

        # Amount mismatch
        task_data["amount"] = 999
        self.mock_escrow.get_task.return_value = task_data
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        self.assertIn("does not match proposal", str(ctx.exception))

        # Recipient mismatch
        task_data["amount"] = proposal["amount"]
        task_data["recipient"] = "0x" + "99" * 20
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        self.assertIn("does not match proposal", str(ctx.exception))

        # Policy hash mismatch
        task_data["recipient"] = proposal["recipient"]
        task_data["policy_hash"] = "0x" + "aa" * 32
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        self.assertIn("does not match", str(ctx.exception))

        # Missing policy
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": task_id})
        self.assertIn("policy", str(ctx.exception).lower())

    async def test_submit_acceptance_fails_if_settlement_reverts(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})

        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)

        self.mock_escrow.settle_mock.side_effect = RuntimeError("execution reverted: InvalidAuthorization")
        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig})
        self.assertIn("Escrow settlement failed on-chain", str(ctx.exception))

    async def test_submit_acceptance_fails_when_vendor_credential_missing(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})

        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)

        self.cred_path.unlink()
        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig})
        self.assertIn("Vendor credential file missing", str(ctx.exception))

    async def test_submit_acceptance_fails_when_vendor_signature_missing(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})

        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)

        self.sig_path.unlink()
        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig})
        self.assertIn("Vendor signature file missing", str(ctx.exception))

    async def test_submit_acceptance_fails_when_vendor_credential_expired(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})

        import yaml

        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)

        c_data = yaml.safe_load(self.cred_path.read_text())
        c_data["valid_until"] = int(time.time()) - 100
        self.cred_path.write_text(yaml.dump(c_data))

        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig})
        self.assertIn("expired", str(ctx.exception).lower())

    async def test_submit_acceptance_fails_when_vendor_credential_scope_mismatches(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})

        import yaml

        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        acceptance = {
            "scope": {
                "chain_id": proposal["chain_id"],
                "vault": list(address20(proposal["escrow_address"])),
                "token": list(address20(proposal["token_address"])),
            },
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(status["deliverable_hash"])),
            "recipient": list(address20(proposal["recipient"])),
            "amount": proposal["amount"],
            "accepted": True,
            "valid_after": buyer_policy["valid_after"],
            "valid_until": buyer_policy["valid_until"],
        }
        sig = sign_acceptance(acceptance)

        c_data = yaml.safe_load(self.cred_path.read_text())
        c_data["scope"]["chain_id"] = 99999
        self.cred_path.write_text(yaml.dump(c_data))

        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig})
        self.assertIn("mismatch", str(ctx.exception).lower())

    async def test_deploy_with_escrow_fails_if_container_provisioning_fails(self):
        proposal = await self.call("propose_deployment")
        task_id = proposal["task_id"]

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data

        with patch.object(
            self.mcp._tool_manager._tools["deploy_with_escrow"].fn.__self__,
            "_provision_container",
            side_effect=ToolError("Container failed to start"),
        ):
            dep_res = await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
            self.assertIn(dep_res["status"], ("provisioning", "failed"))
            import asyncio

            await asyncio.sleep(0.05)
            status = await self.call("get_deployment_status", {"task_id": task_id})
            self.assertEqual(status["status"], "failed")
            self.assertIn("Container failed to start", status["error"])

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

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data

        # Mock subprocess.run for ssh docker calls
        mock_run_results = [
            MagicMock(returncode=0),  # rm -f
            MagicMock(returncode=0, stdout="container-id-123\n", stderr=""),  # run -d
        ]
        mock_health_resp = MagicMock(status_code=200)

        import subprocess as real_subprocess
        real_run = real_subprocess.run

        def mock_ssh_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "ssh":
                return mock_run_results.pop(0) if mock_run_results else MagicMock(returncode=0)
            return real_run(cmd, *args, **kwargs)

        with (
            patch("subprocess.run", side_effect=mock_ssh_run) as mock_subproc,
            patch("httpx2.AsyncClient.get", return_value=mock_health_resp),
        ):
            dep_res = await local_call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
            self.assertIn(dep_res["status"], ("provisioning", "ready"))

            import asyncio

            stat = None
            for _ in range(20):
                await asyncio.sleep(0.05)
                stat = await local_call("get_deployment_status", {"task_id": task_id})
                if stat.get("status") == "ready":
                    break

            self.assertEqual(stat["status"], "ready")
            self.assertEqual(stat["public_url"], "http://petertower:8080")

            ssh_calls = [c for c in mock_subproc.call_args_list if c[0][0][0] == "ssh"]
            self.assertEqual(len(ssh_calls), 2)

            # Check first call: docker rm -f
            first_cmd = ssh_calls[0][0][0]
            self.assertEqual(first_cmd[:5], ["ssh", "petertower", "docker", "rm", "-f"])
            self.assertEqual(first_cmd[5], "warrant-transcription")

            # Check second call: docker run
            second_cmd = ssh_calls[1][0][0]
            second_bin = second_cmd[:5]
            self.assertEqual(second_bin, ["ssh", "petertower", "docker", "run", "-d"])
            self.assertIn("--gpus", second_cmd)
            self.assertIn("all", second_cmd)
            self.assertIn("-p", second_cmd)
            self.assertIn("8080:8080", second_cmd)

            # Check teardown logic
            service = local_mcp._tool_manager._tools["deploy_with_escrow"].fn.__self__
            mock_subproc.reset_mock()
            mock_subproc.side_effect = None
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

        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data

        mock_run_results = [
            MagicMock(returncode=0),  # rm -f
            MagicMock(returncode=1, stderr="docker: permission denied"),  # run failure
        ]

        import subprocess as real_subprocess
        real_run = real_subprocess.run

        def mock_ssh_fail_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd and cmd[0] == "ssh":
                return mock_run_results.pop(0) if mock_run_results else MagicMock(returncode=0)
            return real_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=mock_ssh_fail_run):
            dep_res = await local_call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
            self.assertIn(dep_res["status"], ("provisioning", "failed"))
            import asyncio

            stat = None
            for _ in range(20):
                await asyncio.sleep(0.05)
                stat = await local_call("get_deployment_status", {"task_id": task_id})
                if stat.get("status") == "failed":
                    break
            self.assertEqual(stat["status"], "failed")
            self.assertIn("Failed to start container on petertower", stat["error"])
            self.assertIn("docker: permission denied", stat["error"])

    async def test_multi_session_concurrency_and_routing(self):
        self.mock_escrow.task_id_for.side_effect = lambda customer, salt: f"0x{int(salt, 16):064x}"

        # Generate proposal 1
        p1 = await self.call("propose_deployment")
        t1 = p1["task_id"]

        # Generate proposal 2
        p2 = await self.call("propose_deployment")
        t2 = p2["task_id"]
        self.assertNotEqual(t1, t2)

        policy1, task_data1 = self._create_mock_offered_task(p1)
        policy2, task_data2 = self._create_mock_offered_task(p2)

        def mock_get_task(task_id):
            return task_data1 if task_id == t1 else task_data2

        self.mock_escrow.get_task.side_effect = mock_get_task

        # Deploy both tasks
        res1 = await self.call("deploy_with_escrow", {"task_id": t1, "policy": policy1})
        self.assertIn(res1["status"], ("provisioning", "ready"))
        res2 = await self.call("deploy_with_escrow", {"task_id": t2, "policy": policy2})
        self.assertIn(res2["status"], ("provisioning", "ready"))

        # Check status individually by task_id
        st1 = await self.call("get_deployment_status", {"task_id": t1})
        self.assertEqual(st1["task_id"], t1)
        self.assertTrue(st1["active"])
        self.assertEqual(st1["status"], "ready")

        st2 = await self.call("get_deployment_status", {"task_id": t2})
        self.assertEqual(st2["task_id"], t2)
        self.assertTrue(st2["active"])
        self.assertEqual(st2["status"], "ready")

        # Check transcription routed specifically to t1 and t2
        audio_file = self.state_dir / "sample.mp3"
        audio_file.write_bytes(b"concurrent mock audio")

        res_trans1 = await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file), "task_id": t1})
        self.assertEqual(res_trans1["status"], "complete")

        res_trans2 = await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file), "task_id": t2})
        self.assertEqual(res_trans2["status"], "complete")

        # Default without task_id routes to latest active session
        res_default = await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file)})
        self.assertEqual(res_default["status"], "complete")

        # Nonexistent task_id raises ToolError
        with self.assertRaises(ToolError) as ctx:
            await self.call("transcribe_audio", {"audio_base64": encode_audio(audio_file), "task_id": "0xnonexistent"})
        self.assertIn("No deployment session found for task '0xnonexistent'", str(ctx.exception))

    def test_create_server_and_app_with_config(self):
        config = ServerConfig(
            state_dir=self.state_dir,
            rpc_url="http://127.0.0.1:8545",
            escrow_address="0x" + "11" * 20,
            token_address="0x" + "22" * 20,
            mock_ionet=True,
            auth_user="testuser",
            auth_pass="testpass",
        )
        self.assertIs(ServerConfig, SellerServerConfig)
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            server = create_server(config)
            self.assertIsNotNone(server)
            app = create_app(config)
            self.assertIsNotNone(app)

    def test_read_initial_parameters_from_manifest(self):
        config = ServerConfig(state_dir=self.state_dir)
        self.assertEqual(config.escrow_address, "0xBeD9B731D9C912541FF083AE41607016BFe93b70")
        self.assertEqual(config.verifier_address, "0xDbefc776e18e34049cAaec736b47122f0583F345")
        self.assertEqual(config.buyer_account, "0x9793Dd93D46F153a2A879165cd163A01b54d8A00")
        self.assertEqual(config.rpc_url, "https://rpc.testnet.arc.io")

    def test_read_initial_parameters_from_custom_manifest_kebab_case(self):
        import yaml

        custom_manifest = Path(self.state_dir) / "custom_manifest.yaml"
        custom_manifest.write_text(yaml.dump({
            "rpc-url": "https://custom.rpc.io",
            "escrow-address": "0x" + "12" * 20,
            "verifier-address": "0x" + "34" * 20,
            "buyer-account": "0x" + "56" * 20,
        }))
        config = ServerConfig(state_dir=self.state_dir, manifest_path=custom_manifest)
        self.assertEqual(config.rpc_url, "https://custom.rpc.io")
        self.assertEqual(config.escrow_address, "0x" + "12" * 20)
        self.assertEqual(config.verifier_address, "0x" + "34" * 20)
        self.assertEqual(config.buyer_account, "0x" + "56" * 20)

    def test_explicit_parameters_override_manifest(self):
        config = ServerConfig(
            state_dir=self.state_dir,
            rpc_url="https://override.rpc.io",
            escrow_address="0x" + "99" * 20,
            verifier_address="0x" + "88" * 20,
            buyer_account="0x" + "77" * 20,
        )
        self.assertEqual(config.rpc_url, "https://override.rpc.io")
        self.assertEqual(config.escrow_address, "0x" + "99" * 20)
        self.assertEqual(config.verifier_address, "0x" + "88" * 20)
        self.assertEqual(config.buyer_account, "0x" + "77" * 20)

    async def test_propose_deployment_uses_configured_buyer_account(self):
        custom_buyer = "0x" + "77" * 20
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            local_mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                buyer_account=custom_buyer,
                vendor_credential_path=self.cred_path,
                vendor_signature_path=self.sig_path,
            )
        res = await local_mcp.call_tool("propose_deployment", {})
        proposal = json.loads(res.content[0].text)
        self.assertEqual(proposal["customer"], custom_buyer)

    async def test_propose_deployment_fails_when_customer_address_unconfigured(self):
        empty_manifest = Path(self.state_dir) / "empty_manifest.yaml"
        empty_manifest.write_text(yaml.dump({}))
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            local_mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                vendor_credential_path=self.cred_path,
                vendor_signature_path=self.sig_path,
                manifest_path=empty_manifest,
            )
        with self.assertRaises(ToolError) as ctx:
            await local_mcp.call_tool("propose_deployment", {})
        self.assertIn(
            "Customer address was not provided and is not configured in config/manifest.yaml",
            str(ctx.exception),
        )

    async def test_propose_deployment_fails_when_server_account_unconfigured(self):
        empty_manifest = Path(self.state_dir) / "empty_manifest.yaml"
        empty_manifest.write_text(yaml.dump({}))
        mock_client = MagicMock()
        mock_client.server_account = None
        mock_client.rpc.chain_id.return_value = 31337
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=mock_client):
            local_mcp = create_server(
                self.state_dir,
                mock_ionet=True,
                vendor_credential_path=self.cred_path,
                vendor_signature_path=self.sig_path,
                manifest_path=empty_manifest,
                escrow_client=mock_client,
            )
        with self.assertRaises(ToolError) as ctx:
            await local_mcp.call_tool("propose_deployment", {"customer_address": "0x" + "aa" * 20})
        self.assertIn(
            "Server payout account is not configured in config/manifest.yaml",
            str(ctx.exception),
        )


if __name__ == "__main__":
    unittest.main()
