"""Tests for Warrant io.net transcription MCP server."""

import asyncio
import base64
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from showcases.ionet_arc import seller_mcp_server as seller_module
from showcases.ionet_arc.adapter import PilotError
from showcases.ionet_arc.escrow import DEFAULT_CATEGORY, compile_buyer_policy, find_warrant_binary
from showcases.ionet_arc.init import ensure_key_file, get_public_key_hex
from showcases.ionet_arc.seller_mcp_server import (
    ChainDetectionError,
    MainnetPolicyError,
    SellerServerConfig,
    ServerConfig,
    create_app,
    create_server,
    filter_suitable_gpus,
    parse_credit_balance,
)

KEYS_DIR = Path(__file__).resolve().parent / ".keys"


def keys_dir_snapshot() -> list[str] | None:
    """List the gitignored .keys directory, or None when it does not exist."""
    return sorted(p.name for p in KEYS_DIR.iterdir()) if KEYS_DIR.exists() else None


# Taken at import, before any setUp runs: the tests must never create or change .keys/.
KEYS_DIR_AT_IMPORT = keys_dir_snapshot()


def encode_audio(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def make_throwaway_keys(directory: Path) -> dict:
    """Generate a registry key and a buyer acceptance key for one test run (32 random bytes as hex)."""
    registry_key = directory / "registry.key"
    acceptance_key = directory / "buyer_acceptance.key"
    ensure_key_file(registry_key, force=True)
    ensure_key_file(acceptance_key, force=True)
    evidence_bin = find_warrant_binary("warrant-evidence")
    return {
        "registry_key": registry_key,
        "acceptance_key": acceptance_key,
        "registry_public_key": get_public_key_hex(evidence_bin, registry_key),
        "acceptance_public_key": get_public_key_hex(evidence_bin, acceptance_key),
    }


def make_offered_task(proposal: dict, keys: dict) -> tuple[dict, dict]:
    """Compile the buyer policy for a proposal with the throwaway keys and build the matching on-chain task."""
    buyer_policy, policy_hash = compile_buyer_policy(
        chain_id=proposal["chain_id"],
        escrow_address=proposal["escrow_address"],
        token_address=proposal["token_address"],
        max_amount=proposal["amount"],
        registry_public_key=keys["registry_public_key"],
        acceptance_public_key=keys["acceptance_public_key"],
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


def write_vendor_credential(directory: Path, escrow, chain_id: int, keys: dict) -> tuple[Path, Path]:
    """Sign a vendor credential for the mock escrow scope with the throwaway registry key."""
    from showcases.ionet_arc.escrow import address20, sign_vendor_credential

    now = int(time.time())
    cred_obj = {
        "scope": {
            "chain_id": chain_id,
            "vault": list(address20(escrow.escrow)),
            "token": list(address20(escrow.token)),
        },
        "recipient": list(address20(escrow.server_account)),
        "category": DEFAULT_CATEGORY,
        "valid_after": now - 3600,
        "valid_until": now + 86400,
    }
    sig_bytes = sign_vendor_credential(cred_obj, key_path=keys["registry_key"])
    cred_path = directory / "vendor_credential.yaml"
    sig_path = directory / "vendor_signature.yaml"
    cred_path.write_text(yaml.dump({
        "scope": {"chain_id": chain_id, "vault": escrow.escrow, "token": escrow.token},
        "recipient": escrow.server_account,
        "category": DEFAULT_CATEGORY,
        "valid_after": cred_obj["valid_after"],
        "valid_until": cred_obj["valid_until"],
    }))
    sig_path.write_text(yaml.dump({"signature": "0x" + bytes(sig_bytes).hex()}))
    return cred_path, sig_path


LABEL = "after seller acceptance anyone can trigger payout; no proof; amounts up to 1 USD"
LIMITS_SENTENCES = (
    "Settlement uses a test-double verifier that accepts any digest the seller approves.",
    "The 1 USD cap and the one-hour lease are seller policy, not contract rules.",
    "One deal at a time.",
    "The mainnet seller path has not been exercised with money before this listing.",
    "Any holder of the basic-auth credential can read the current deployment status, including its worker token.",
)
SELLER_TOOLS = {
    "list_suitable_hardware",
    "propose_deployment",
    "deploy_with_escrow",
    "transcribe_audio",
    "get_deployment_status",
    "submit_acceptance",
}
# Tool descriptions on origin/main before Task 2; chains other than 5042 keep them exactly.
TESTNET_DESCRIPTIONS = {
    "list_suitable_hardware": (
        "List available io.net GPU hardware options suitable for faster-whisper-small transcription.\n"
        "Filters for single-GPU configurations with live availability and sorts by price ascending."
    ),
    "propose_deployment": (
        "Generate an escrow proposal to deploy the transcription container for 1 hour duration.\n"
        "Selects the cheapest suitable available GPU (or a specific hardware_id), calculates pricing, "
        "and returns concrete TaskEscrow payment terms (taskId, salt, amount, recipient)."
    ),
    "deploy_with_escrow": (
        "Verify on-chain escrow funding for the proposed task, accept it on-chain, and provision the "
        "io.net container in the background for 1 hour duration.\n"
        "Prerequisite: propose_deployment must have been called and TaskEscrow.offer executed on-chain."
    ),
    "transcribe_audio": (
        "Transcribe audio (passed as base64-encoded bytes in audio_base64) using an active 1-hour "
        "Whisper deployment on io.net.\n"
        "Prerequisite: deploy_with_escrow must have been completed and the 1-hour window must not be "
        "expired.\n"
        "Optionally provide task_id to target a specific deployment when multiple tasks exist."
    ),
    "get_deployment_status": (
        "Check status of active 1-hour transcription deployment and remaining duration.\n"
        "Optionally provide task_id to inspect a specific deployment session."
    ),
    "submit_acceptance": (
        "Submit buyer-signed Acceptance evidence for a verified deliverable.\n"
        "Evaluates policy and signatures off-chain via Warrant, approves the 384-byte journal, and "
        "settles payment on TaskEscrow on-chain."
    ),
}
LABELLED_TOOLS = {"propose_deployment", "deploy_with_escrow", "submit_acceptance"}
MAINNET_BUYER = "0x" + "77" * 20
MAINNET_HARDWARE = [
    {
        "hardware_id": 21,
        "hardware_name": "RTX A6000",
        "price": 0.49,
        "available": 3,
        "max_gpus_per_container": 1,
        "location": "US",
    },
    {
        "hardware_id": 40,
        "hardware_name": "L40",
        "price": 1.00,
        "available": 2,
        "max_gpus_per_container": 1,
        "location": "US",
    },
    {
        "hardware_id": 41,
        "hardware_name": "A100",
        "price": 1.01,
        "available": 2,
        "max_gpus_per_container": 1,
        "location": "US",
    },
    {
        "hardware_id": 30,
        "hardware_name": "H100",
        "price": 1.50,
        "available": 2,
        "max_gpus_per_container": 1,
        "location": "US",
    },
]
EXAMPLE_MANIFEST = Path(__file__).resolve().parent / "config" / "mainnet-manifest.example.yaml"


def credit_status_response(credits_balance, courtesy_balance=0.0) -> dict:
    """io.net get_credit_status result in the real shape (read 2026-10-15; amounts are test values)."""
    return {
        "service": "io-cloud",
        "operation": "get_credit_status",
        "data": {
            "balances": {
                "cloud_balance": 0.0,
                "cloud_iocoin_balance": 0.0,
                "worker_io_coin_balance": 0.0,
                "credits_balance": credits_balance,
                "credits_withdrawable_balance": credits_balance,
                "credits_courtesy_balance": courtesy_balance,
                "credits_used": 1.32,
                "all_credits": 10.0,
                "purchased_credits_exempt": False,
            },
            "burn_rate": {"hourly_rate": None, "runway_hours_estimate": None, "cluster_count": 0},
        },
    }


class FakeCloud:
    """Answers the io.net cloud calls that the seller makes on chain 5042."""

    def __init__(self):
        self.hardware = list(MAINNET_HARDWARE)
        self.credit_result: dict | Exception = credit_status_response(10.0)
        self.calls: list[str] = []

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "caas_get_hardware_ids":
            return {"data": {"data": list(self.hardware)}}
        if name == "get_credit_status":
            if isinstance(self.credit_result, Exception):
                raise self.credit_result
            return self.credit_result
        raise AssertionError(f"unexpected io.net call {name}")


class WarrantTranscriptionServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        self.keys = make_throwaway_keys(self.state_dir)

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
        self.cred_path, self.sig_path = write_vendor_credential(self.state_dir, self.mock_escrow, 31337, self.keys)

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
        return make_offered_task(proposal, self.keys)

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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])
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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])

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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])

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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])

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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])

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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])

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


    async def test_registry_key_generated_per_run(self):
        for name in ("registry_key", "acceptance_key"):
            key_path = self.keys[name]
            self.assertEqual(key_path.parent, self.state_dir)
            key_hex = key_path.read_text().strip()
            self.assertRegex(key_hex, r"^[0-9a-f]{64}$")
        other = make_throwaway_keys(Path(tempfile.mkdtemp(dir=self.state_dir)))
        self.assertNotEqual(other["registry_key"].read_text(), self.keys["registry_key"].read_text())
        # Nothing under showcases/ionet_arc/.keys/ was created or changed by the fixtures.
        self.assertEqual(keys_dir_snapshot(), KEYS_DIR_AT_IMPORT)

    async def test_testnet_descriptions_and_instructions_unchanged(self):
        servers = {31337: self.mcp}
        testnet = MagicMock()
        testnet.escrow, testnet.token = self.mock_escrow.escrow, self.mock_escrow.token
        testnet.verifier, testnet.server_account = self.mock_escrow.verifier, self.mock_escrow.server_account
        testnet.rpc.chain_id.return_value = 5042002
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=testnet):
            servers[5042002] = create_server(self.state_dir / "testnet", mock_ionet=True)
        for chain_id, server in servers.items():
            with self.subTest(chain_id=chain_id):
                async with Client(server) as client:
                    self.assertIsNone(client.instructions)
                    tools = (await client.list_tools()).tools
                self.assertEqual({t.name for t in tools}, SELLER_TOOLS)
                self.assertEqual({t.name: t.description for t in tools}, TESTNET_DESCRIPTIONS)
                for tool in tools:
                    self.assertNotIn(LABEL, tool.description)
                    self.assertNotIn("anyone can trigger payout", tool.description)

    async def test_testnet_task_id_behaviour_unchanged(self):
        # Empty task_id keeps the current results on 31337: no mainnet refusal.
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": "", "policy": {"version": 1}})
        self.assertIn("No proposal record found for task_id ''", str(ctx.exception))
        self.assertNotIn("required on chain 5042", str(ctx.exception))

        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": "", "acceptance": {}, "signature": []})
        self.assertIn("No deployment session found for task ''", str(ctx.exception))
        self.assertNotIn("required on chain 5042", str(ctx.exception))

        missing = await self.call("get_deployment_status")
        self.assertEqual(missing["status"], "missing")

        proposal = await self.call("propose_deployment")
        buyer_policy, task_data = self._create_mock_offered_task(proposal)
        self.mock_escrow.get_task.return_value = task_data
        await self.call("deploy_with_escrow", {"task_id": proposal["task_id"], "policy": buyer_policy})

        # Without task_id the status falls back to the latest session.
        latest = await self.call("get_deployment_status")
        self.assertEqual(latest["task_id"], proposal["task_id"])
        self.assertEqual(latest["status"], "ready")

    async def test_testnet_live_mode_has_no_credit_check_or_cap(self):
        cloud = FakeCloud()

        async def fake_connected(operation):
            return await operation(cloud)

        with (
            patch.dict(os.environ, {"IO_NET_API_KEY": "test-dummy-key"}),
            patch("showcases.ionet_arc.seller_mcp_server.connected", side_effect=fake_connected),
            patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow),
        ):
            live = create_server(self.state_dir, mock_ionet=False)
            res = await live.call_tool(
                "propose_deployment", {"hardware_id": 30, "duration_hours": 2, "budget_cap_usd": None}
            )
        proposal = json.loads(res.content[0].text)
        self.assertEqual(proposal["amount"], 3_000_000)
        self.assertEqual(cloud.calls, ["caas_get_hardware_ids"])

    def test_main_exits_with_status_1_on_mainnet_policy_error(self):
        stderr = io.StringIO()
        refusal = MainnetPolicyError("mock mode is refused on chain 5042")
        with (
            patch.object(seller_module, "run_server", side_effect=refusal),
            patch("sys.argv", ["seller_mcp_server"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            seller_module.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("mock mode is refused on chain 5042", stderr.getvalue())

    def test_main_exits_with_status_1_when_chain_unknown(self):
        stderr = io.StringIO()
        with (
            patch.object(seller_module, "run_server", side_effect=ChainDetectionError("cannot determine the chain")),
            patch("sys.argv", ["seller_mcp_server"]),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            seller_module.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("cannot determine the chain", stderr.getvalue())

    def test_main_keeps_other_errors(self):
        with (
            patch.object(seller_module, "run_server", side_effect=RuntimeError("other failure")),
            patch("sys.argv", ["seller_mcp_server"]),
            self.assertRaises(RuntimeError),
        ):
            seller_module.main()

class MainnetSellerPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Decision 6: the seller policy on RPC chain 5042."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name) / "state"
        self.keys = make_throwaway_keys(Path(self.tmp.name))

        self.mock_escrow = MagicMock()
        self.mock_escrow.escrow = "0x" + "11" * 20
        self.mock_escrow.token = "0x3600000000000000000000000000000000000000"
        self.mock_escrow.verifier = "0x" + "33" * 20
        self.mock_escrow.server_account = "0x" + "44" * 20
        self.mock_escrow.rpc.chain_id.return_value = 5042
        self.mock_escrow.task_id_for.side_effect = lambda customer, salt: f"0x{int(salt, 16):064x}"
        self.mock_escrow.settle_mock.return_value = {"transactionHash": "0x" + "aa" * 32}
        self.offered: dict[str, dict] = {}
        self.mock_escrow.get_task.side_effect = lambda task_id: self.offered.get(task_id, {"state": "Missing"})

        manifest = yaml.safe_load(EXAMPLE_MANIFEST.read_text())
        self.assertEqual(manifest["chain_id"], 5042)
        self.manifest_path = Path(self.tmp.name) / "mainnet-manifest.yaml"
        self.manifest_path.write_text(yaml.dump(manifest))
        self.cred_path, self.sig_path = write_vendor_credential(Path(self.tmp.name), self.mock_escrow, 5042, self.keys)

        env = patch.dict(os.environ, {"IO_NET_API_KEY": "test-dummy-key"})
        env.start()
        self.addCleanup(env.stop)

        self.cloud = FakeCloud()

        async def fake_connected(operation):
            return await operation(self.cloud)

        self.connected = MagicMock(side_effect=fake_connected)
        cloud_patch = patch("showcases.ionet_arc.seller_mcp_server.connected", self.connected)
        cloud_patch.start()
        self.addCleanup(cloud_patch.stop)

        self.mcp = self.build()

    def build(self, **overrides):
        kwargs = {
            "mock_ionet": False,
            "vendor_credential_path": self.cred_path,
            "vendor_signature_path": self.sig_path,
            "manifest_path": self.manifest_path,
            "buyer_account": MAINNET_BUYER,
        }
        kwargs.update(overrides)
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            return create_server(self.state_dir, **kwargs)

    @staticmethod
    def service_of(server):
        # The only place that reaches into mcp 2.3.0 private attributes; the existing tests use the same path.
        return server._tool_manager._tools["deploy_with_escrow"].fn.__self__

    async def call(self, tool_name: str, arguments: dict | None = None, server=None) -> dict:
        res = await (server or self.mcp).call_tool(tool_name, arguments or {})
        return json.loads(res.content[0].text)

    async def offered_proposal(self, server=None) -> tuple[dict, dict]:
        """Propose a deal and mark it as offered on-chain; return the proposal and the buyer policy."""
        proposal = await self.call("propose_deployment", server=server)
        buyer_policy, task_data = make_offered_task(proposal, self.keys)
        self.offered[proposal["task_id"]] = task_data
        return proposal, buyer_policy

    def write_session(self, task_id: str, **fields) -> Path:
        session = {"task_id": task_id, "status": "provisioning", "public_url": None, **fields}
        path = self.state_dir / "sessions" / f"{task_id}.json"
        path.write_text(json.dumps(session))
        return path

    async def wait_background(self, server=None):
        """Wait until the background provisioning tasks of the server have finished."""
        tasks = list(self.service_of(server or self.mcp)._bg_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

    def provision_ready(self, server=None):
        """Replace the io.net provisioning with an immediate ready container."""
        return patch.object(
            self.service_of(server or self.mcp),
            "_provision_container",
            return_value="https://worker.example.invalid",
        )

    # Start guard

    def test_mainnet_refuses_missing_ionet_key(self):
        with patch.dict(os.environ, clear=True), self.assertRaises(MainnetPolicyError) as ctx:
            self.build()
        self.assertIn("IO_NET_API_KEY is required on chain 5042", str(ctx.exception))

    def test_mainnet_starts_with_ionet_key(self):
        service = self.service_of(self.build())
        self.assertFalse(service.use_mock_ionet)
        self.assertTrue(service.mainnet)
        self.assertEqual(service.chain_id, 5042)
        self.connected.assert_not_called()

    def test_mainnet_refuses_mock_ionet(self):
        with self.assertRaises(MainnetPolicyError) as ctx:
            self.build(mock_ionet=True)
        self.assertIn("mock mode is refused on chain 5042", str(ctx.exception))
        with patch.dict(os.environ, clear=True), self.assertRaises(MainnetPolicyError):
            self.build(mock_ionet=True)

    def test_mainnet_refuses_local_server(self):
        with self.assertRaises(MainnetPolicyError) as ctx:
            self.build(local_server=True)
        self.assertIn("local mode is refused on chain 5042", str(ctx.exception))

    def test_mainnet_refuses_manifest_chain_mismatch(self):
        testnet_manifest = Path(self.tmp.name) / "testnet-manifest.yaml"
        testnet_manifest.write_text(yaml.dump({**yaml.safe_load(self.manifest_path.read_text()), "chain_id": 5042002}))
        with self.assertRaises(MainnetPolicyError) as ctx:
            self.build(manifest_path=testnet_manifest)
        self.assertIn("5042002", str(ctx.exception))
        self.assertIn("5042", str(ctx.exception).replace("5042002", ""))
        self.assertIsNotNone(self.build(manifest_path=self.manifest_path))

    def test_mainnet_refuses_manifest_without_chain_id(self):
        no_chain = Path(self.tmp.name) / "no-chain.yaml"
        no_chain.write_text(yaml.dump({"escrow_address": self.mock_escrow.escrow}))
        for manifest_path in (no_chain, Path(self.tmp.name) / "missing.yaml"):
            with self.subTest(manifest=manifest_path.name), self.assertRaises(MainnetPolicyError) as ctx:
                self.build(manifest_path=manifest_path)
            self.assertIn("chain_id", str(ctx.exception))

    def test_start_refused_when_rpc_chain_unreadable(self):
        self.mock_escrow.rpc.chain_id.side_effect = RuntimeError("connection refused")
        with self.assertRaises(ChainDetectionError) as ctx:
            self.build()
        self.assertIn("cannot determine the chain", str(ctx.exception))
        self.assertNotIn("connection refused", str(ctx.exception))

    # Label and limits through the protocol

    async def test_mainnet_initialize_instructions_carry_label_and_limits(self):
        async with Client(self.mcp) as client:
            instructions = client.instructions
        self.assertIn(LABEL, instructions)
        for sentence in LIMITS_SENTENCES:
            self.assertIn(sentence, instructions)
        self.connected.assert_not_called()

    async def test_mainnet_deal_tool_descriptions_start_with_label(self):
        async with Client(self.mcp) as client:
            tools = {t.name: t.description for t in (await client.list_tools()).tools}
        self.assertEqual(set(tools), SELLER_TOOLS)
        for name in LABELLED_TOOLS:
            self.assertTrue(tools[name].startswith(LABEL), name)
        for name in SELLER_TOOLS - LABELLED_TOOLS:
            self.assertNotIn(LABEL, tools[name], name)
        # get_credit_status is an io.net cloud call, never a seller tool, so it carries no label.
        self.assertNotIn("get_credit_status", tools)
        # Construction, initialize and tools/list make no io.net call (Tasks 3 and 13 rely on this).
        self.connected.assert_not_called()

    # Deal cap

    async def test_mainnet_budget_cap_cannot_exceed_one_usd(self):
        for cap in (5, "5", None):
            with self.subTest(budget_cap_usd=cap), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment", {"hardware_id": 30, "budget_cap_usd": cap})
            self.assertIn("1.00", str(ctx.exception))
        proposal = await self.call("propose_deployment", {"hardware_id": 21, "budget_cap_usd": 5})
        self.assertEqual(proposal["amount"], 490_000)
        self.assertEqual(proposal["duration_hours"], 1)
        self.assertEqual(proposal["chain_id"], 5042)

    async def test_mainnet_cap_boundary_is_one_usd(self):
        at_cap = await self.call("propose_deployment", {"hardware_id": 40})
        self.assertEqual(at_cap["amount"], 1_000_000)
        explicit = await self.call("propose_deployment", {"hardware_id": 21, "budget_cap_usd": "1.00"})
        self.assertEqual(explicit["amount"], 490_000)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"hardware_id": 41, "budget_cap_usd": "1.01"})
        self.assertIn("1.00", str(ctx.exception))

    async def test_mainnet_budget_cap_below_one_usd_is_respected(self):
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"hardware_id": 21, "budget_cap_usd": "0.40"})
        self.assertIn("exceeds", str(ctx.exception))
        for invalid in ("abc", "NaN", "Infinity"):
            with self.subTest(budget_cap_usd=invalid), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment", {"budget_cap_usd": invalid})
            self.assertIn("Invalid budget_cap_usd", str(ctx.exception))

    async def test_mainnet_refuses_hardware_without_valid_price(self):
        # The policy refuses non-finite prices; filter_suitable_gpus already drops zero and negative prices,
        # and a text price fails in the existing filter. Every case ends without a proposal.
        expected = {
            float("nan"): "no valid price",
            float("inf"): "no valid price",
            0: "No available suitable GPUs",
            -0.5: "No available suitable GPUs",
            "0.49": None,  # fails in the existing filter; only the refusal matters
        }
        for price, message in expected.items():
            self.cloud.hardware = [{**MAINNET_HARDWARE[0], "price": price}]
            with self.subTest(price=price), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment")
            if message:
                self.assertIn(message, str(ctx.exception))
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])

    async def test_mainnet_cheapest_gpu_above_cap_is_refused(self):
        self.cloud.hardware = [hw for hw in MAINNET_HARDWARE if hw["price"] > 1.00]
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment")
        self.assertIn("1.00", str(ctx.exception))
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])

    async def test_mainnet_refuses_duration_other_than_one_hour(self):
        for hours in (2, 0):
            with self.subTest(duration_hours=hours), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment", {"duration_hours": hours})
            self.assertIn("1 hour", str(ctx.exception))
        # The tool schema coerces protocol input to int; the service also refuses a direct non-int value.
        service = self.service_of(self.mcp)
        for hours in (True, 1.0):
            with self.subTest(direct_duration_hours=hours), self.assertRaises(ToolError) as ctx:
                await service.propose_deployment(duration_hours=hours)
            self.assertIn("1 hour", str(ctx.exception))
        # Refused before the hardware call and before the credit call.
        self.assertEqual(self.cloud.calls, [])

    # Credits

    async def test_mainnet_low_credits_refuse_proposal(self):
        self.cloud.credit_result = credit_status_response(1.99)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment")
        self.assertIn("credit", str(ctx.exception).lower())
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])
        self.mock_escrow.task_id_for.assert_not_called()

    async def test_mainnet_sufficient_credits_allow_proposal(self):
        self.cloud.credit_result = credit_status_response(2.5)
        proposal = await self.call("propose_deployment")
        self.assertEqual(proposal["status"], "proposed")
        self.assertIn("get_credit_status", self.cloud.calls)
        self.assertTrue((self.state_dir / "proposals" / f"{proposal['task_id']}.json").exists())

    async def test_mainnet_credits_exactly_at_floor_allow_proposal(self):
        self.cloud.credit_result = credit_status_response(2.0)
        proposal = await self.call("propose_deployment")
        self.assertEqual(proposal["status"], "proposed")

    async def test_mainnet_unreadable_credits_refuse_proposal(self):
        unreadable = (
            {},
            {"unexpected": "shape"},
            credit_status_response("8.68"),
            credit_status_response(float("nan")),
            credit_status_response(float("inf")),
            credit_status_response(-5.0),
            PilotError("io.net down"),
        )
        for result in unreadable:
            self.cloud.credit_result = result
            with self.subTest(result=repr(result)), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment")
            self.assertIn("credit", str(ctx.exception).lower())
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])

    async def test_mainnet_courtesy_credits_do_not_count(self):
        # 3.00 credits of which 1.50 are courtesy: 1.50 spendable is below the floor.
        self.cloud.credit_result = credit_status_response(3.0, courtesy_balance=1.5)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment")
        self.assertIn("below 2 USD", str(ctx.exception))
        # Spendable exactly 2.00 after courtesy is allowed; 1.99 is refused.
        self.cloud.credit_result = credit_status_response(3.5, courtesy_balance=1.5)
        self.assertEqual((await self.call("propose_deployment"))["status"], "proposed")
        self.cloud.credit_result = credit_status_response(3.49, courtesy_balance=1.5)
        with self.assertRaises(ToolError):
            await self.call("propose_deployment")

    def test_parse_credit_balance_real_shape(self):
        self.assertEqual(parse_credit_balance(credit_status_response(8.68)), Decimal("8.68"))
        self.assertEqual(parse_credit_balance(credit_status_response(3.0, courtesy_balance=1.25)), Decimal("1.75"))
        self.assertEqual(parse_credit_balance(credit_status_response(0)), Decimal("0"))

    def test_parse_credit_balance_unreadable_fails_closed(self):
        valid = credit_status_response(8.68)
        balances = valid["data"]["balances"]
        without_courtesy = {k: v for k, v in balances.items() if k != "credits_courtesy_balance"}
        cases = {
            "not a dict": [],
            "no data": {"service": "io-cloud"},
            "data not a dict": {"data": "x"},
            "no balances": {"data": {"burn_rate": {}}},
            "credits_balance missing": {"data": {"balances": {"credits_courtesy_balance": 0.0}}},
            "courtesy missing": {"data": {"balances": without_courtesy}},
            "credits_balance bool": credit_status_response(True),
            "credits_balance string": credit_status_response("8.68"),
            "credits_balance null": credit_status_response(None),
            "credits_balance NaN": credit_status_response(float("nan")),
            "credits_balance Infinity": credit_status_response(float("inf")),
            "credits_balance negative": credit_status_response(-0.01),
            "courtesy negative": credit_status_response(8.68, courtesy_balance=-1.0),
            "courtesy bool": credit_status_response(8.68, courtesy_balance=False),
            "courtesy above balance": credit_status_response(1.0, courtesy_balance=2.0),
        }
        for case, status in cases.items():
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_credit_balance(status)

    # One active session

    async def test_mainnet_single_active_session(self):
        p1, policy1 = await self.offered_proposal()
        p2, policy2 = await self.offered_proposal()
        p3, policy3 = await self.offered_proposal()
        with self.provision_ready():
            first = await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
            self.assertEqual(first["status"], "ready")

            with self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2})
            self.assertIn(p1["task_id"], str(ctx.exception))
            self.mock_escrow.accept.assert_called_once_with(p1["task_id"], sender=p1["recipient"])

            # The first session expires: the second deal is allowed.
            session_path = self.state_dir / "sessions" / f"{p1['task_id']}.json"
            expired = {**json.loads(session_path.read_text()), "expires_at": int(time.time()) - 1}
            session_path.write_text(json.dumps(expired))
            second = await self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2})
            self.assertEqual(second["status"], "ready")

            # The second session fails: the third deal is allowed.
            self.write_session(p2["task_id"], status="failed", error="container failed")
            await self.call("deploy_with_escrow", {"task_id": p3["task_id"], "policy": policy3})
        accepted = [c.args[0] for c in self.mock_escrow.accept.call_args_list]
        self.assertEqual(accepted, [p1["task_id"], p2["task_id"], p3["task_id"]])

    async def test_mainnet_settled_session_with_running_lease_blocks(self):
        p1, policy1 = await self.offered_proposal()
        self.write_session("0x" + "ab" * 32, status="settled", expires_at=int(time.time()) + 600)
        with self.assertRaises(ToolError):
            await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_provisioning_session_counts_as_active(self):
        blocking = "0x" + "cd" * 32
        self.write_session(blocking, status="provisioning", public_url=None)
        p1, policy1 = await self.offered_proposal()
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
        self.assertIn(blocking, str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_unreadable_session_file_blocks(self):
        bad = self.state_dir / "sessions" / "broken.json"
        bad.write_text("{not json")
        p1, policy1 = await self.offered_proposal()
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
        self.assertIn("broken.json", str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_session_from_before_restart_blocks(self):
        old_task = "0x" + "ef" * 32
        newer_task = "0x" + "fe" * 32
        self.write_session(
            old_task, status="ready", public_url="https://old.example.invalid", expires_at=int(time.time()) + 1800
        )
        failed = self.write_session(newer_task, status="failed", error="container failed")
        (self.state_dir / "active_session.json").write_text(failed.read_text())

        restarted = self.build()
        proposal, policy = await self.offered_proposal(server=restarted)
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": proposal["task_id"], "policy": policy}, server=restarted)
        self.assertIn(old_task, str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_concurrent_deploys_accept_once(self):
        p1, policy1 = await self.offered_proposal()
        p2, policy2 = await self.offered_proposal()
        self.mock_escrow.accept.side_effect = lambda task_id, sender=None: time.sleep(0.05)
        with self.provision_ready():
            results = await asyncio.gather(
                self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1}),
                self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2}),
                return_exceptions=True,
            )
        errors = [r for r in results if isinstance(r, Exception)]
        successes = [r for r in results if not isinstance(r, Exception)]
        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(errors), 1, results)
        self.assertIsInstance(errors[0], ToolError)
        self.assertIn("One deal at a time", str(errors[0]))
        self.mock_escrow.accept.assert_called_once()
        self.assertEqual(self.mock_escrow.accept.call_args.args[0], successes[0]["task_id"])

    async def test_mainnet_failed_step_before_save_releases_slot(self):
        p1, policy1 = await self.offered_proposal()
        self.mock_escrow.accept.side_effect = RuntimeError("execution reverted")
        with self.assertRaises(ToolError):
            await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
        self.mock_escrow.accept.side_effect = None
        with self.provision_ready():
            result = await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
        self.assertEqual(result["status"], "ready")

    async def test_mainnet_session_written_by_deploy_while_provisioning_blocks(self):
        p1, policy1 = await self.offered_proposal()
        p2, policy2 = await self.offered_proposal()
        release = asyncio.Event()

        async def slow_provision(*_args):
            await release.wait()
            return "https://worker.example.invalid"

        with patch.object(self.service_of(self.mcp), "_provision_container", side_effect=slow_provision):
            try:
                first = await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
                self.assertEqual(first["status"], "provisioning")
                written = json.loads((self.state_dir / "sessions" / f"{p1['task_id']}.json").read_text())
                self.assertEqual(written["status"], "provisioning")
                self.assertNotIn("expires_at", written)
                self.assertIsNone(written["public_url"])
                with self.assertRaises(ToolError) as ctx:
                    await self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2})
                self.assertIn(p1["task_id"], str(ctx.exception))
                self.mock_escrow.accept.assert_called_once_with(p1["task_id"], sender=p1["recipient"])
            finally:
                release.set()
                await self.wait_background()

    async def test_mainnet_failed_session_with_ionet_deployment_blocks_until_lease_ends(self):
        p1, policy1 = await self.offered_proposal()
        p2, policy2 = await self.offered_proposal()
        service = self.service_of(self.mcp)

        async def deploy_then_fail(proposal, deployment_id, worker_token):
            # deploy() saves its state file before the remote call; then the provisioning fails.
            (self.state_dir / f"{deployment_id}.json").write_text(json.dumps({"phase": "submission_unknown"}))
            raise PilotError("io.net timeout after submission")

        with patch.object(service, "_provision_container", side_effect=deploy_then_fail):
            await self.call("deploy_with_escrow", {"task_id": p1["task_id"], "policy": policy1})
            await self.wait_background()
        session_path = self.state_dir / "sessions" / f"{p1['task_id']}.json"
        session = json.loads(session_path.read_text())
        self.assertEqual(session["status"], "failed")
        self.assertTrue(session["deployment_may_exist"])
        self.assertGreater(session["expires_at"], time.time() + 3500)

        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2})
        self.assertIn(p1["task_id"], str(ctx.exception))
        self.assertEqual(self.mock_escrow.accept.call_count, 1)

        # After the lease could have ended, the slot opens again.
        session_path.write_text(json.dumps({**session, "expires_at": int(time.time()) - 1}))
        with self.provision_ready():
            await self.call("deploy_with_escrow", {"task_id": p2["task_id"], "policy": policy2})
        self.assertEqual(self.mock_escrow.accept.call_count, 2)

    async def test_mainnet_stored_proposal_outside_limits_is_not_accepted(self):
        proposal, policy = await self.offered_proposal()
        prop_path = self.state_dir / "proposals" / f"{proposal['task_id']}.json"
        stored = json.loads(prop_path.read_text())
        tampered = (
            {"amount": 2_000_000},
            {"duration_hours": 2},
            {"chain_id": 5042002},
            {"amount": "490000"},
        )
        for change in tampered:
            prop_path.write_text(json.dumps({**stored, **change}))
            with self.subTest(change=change), self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": proposal["task_id"], "policy": policy})
            self.assertIn("limits", str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_doubtful_session_files_block(self):
        future = int(time.time()) + 600
        cases = {
            "non-dict JSON": "[1, 2]",
            "unknown status": json.dumps({"task_id": "0x01", "status": "paused", "expires_at": future}),
            "ready without expires_at": json.dumps({"task_id": "0x02", "status": "ready"}),
            "settled with text expires_at": json.dumps({"task_id": "0x03", "status": "settled", "expires_at": "soon"}),
            "ready with boolean expires_at": json.dumps({"task_id": "0x04", "status": "ready", "expires_at": True}),
        }
        proposal, policy = await self.offered_proposal()
        doubtful = self.state_dir / "sessions" / "doubtful.json"
        for case, content in cases.items():
            doubtful.write_text(content)
            with self.subTest(case=case), self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": proposal["task_id"], "policy": policy})
            self.assertIn("One deal at a time", str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_full_deal_with_task_id(self):
        from showcases.ionet_arc.escrow import address20, bytes32, sign_acceptance

        proposal, buyer_policy = await self.offered_proposal()
        task_id = proposal["task_id"]
        self.assertRegex(task_id, r"^0x[0-9a-f]{64}$")
        with self.provision_ready():
            await self.call("deploy_with_escrow", {"task_id": task_id, "policy": buyer_policy})
        status = await self.call("get_deployment_status", {"task_id": task_id})
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["task_id"], task_id)
        # Upper-case hex names the same task.
        upper = await self.call("get_deployment_status", {"task_id": "0x" + task_id[2:].upper()})
        self.assertEqual(upper["task_id"], task_id)

        acceptance = {
            "scope": {
                "chain_id": 5042,
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
        sig = sign_acceptance(acceptance, key_path=self.keys["acceptance_key"])
        settled = await self.call(
            "submit_acceptance", {"task_id": task_id, "acceptance": acceptance, "signature": sig}
        )
        self.assertEqual(settled["status"], "settled")
        self.mock_escrow.settle_mock.assert_called_once()

    # task_id required

    async def test_mainnet_task_id_required(self):
        calls = (
            ("get_deployment_status", {}),
            ("get_deployment_status", {"task_id": ""}),
            ("deploy_with_escrow", {"task_id": "", "policy": {"version": 1}}),
            ("submit_acceptance", {"task_id": "", "acceptance": {}, "signature": []}),
        )
        for tool, args in calls:
            with self.subTest(tool=tool, args=args), self.assertRaises(ToolError) as ctx:
                await self.call(tool, args)
            self.assertIn("task_id is required on chain 5042", str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_mainnet_task_id_must_be_a_task_hash(self):
        for task_id in ("../active_session", "0x1234", "0x" + "zz" * 32):
            with self.subTest(task_id=task_id), self.assertRaises(ToolError) as ctx:
                await self.call("get_deployment_status", {"task_id": task_id})
            self.assertIn("32-byte hex", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
