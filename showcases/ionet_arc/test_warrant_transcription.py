"""Tests for Warrant io.net transcription MCP server."""

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
from showcases.ionet_arc.escrow import (
    DEFAULT_CATEGORY,
    DEFAULT_MANIFEST_PATH,
    compile_buyer_policy,
    find_warrant_binary,
)
from showcases.ionet_arc.init import ensure_key_file, get_public_key_hex
from showcases.ionet_arc.seller_mcp_server import (
    ChainDetectionError,
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
    cred_path.write_text(
        yaml.dump(
            {
                "scope": {"chain_id": chain_id, "vault": escrow.escrow, "token": escrow.token},
                "recipient": escrow.server_account,
                "category": DEFAULT_CATEGORY,
                "valid_after": cred_obj["valid_after"],
                "valid_until": cred_obj["valid_until"],
            }
        )
    )
    sig_path.write_text(yaml.dump({"signature": "0x" + bytes(sig_bytes).hex()}))
    return cred_path, sig_path


SELLER_TOOLS = {
    "list_suitable_hardware",
    "propose_deployment",
    "deploy_with_escrow",
    "get_deployment_status",
    "submit_acceptance",
}
SAMPLE_BUYER = "0x" + "77" * 20
SAMPLE_HARDWARE = [
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
        self.hardware = list(SAMPLE_HARDWARE)
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

        self.cloud = FakeCloud()

        async def fake_connected(op):
            return await op(self.cloud)

        patch_conn = patch("showcases.ionet_arc.seller_mcp_server.connected", side_effect=fake_connected)
        patch_conn.start()
        self.addCleanup(patch_conn.stop)

        orig_provision = seller_module.SellerWarrantService._provision_container

        async def fake_provision(service_self, proposal, deployment_id, worker_token):
            if service_self.local_server:
                return await orig_provision(service_self, proposal, deployment_id, worker_token)
            return "http://127.0.0.1:8080"

        patch_prov = patch.object(
            seller_module.SellerWarrantService,
            "_provision_container",
            autospec=True,
            side_effect=fake_provision,
        )
        patch_prov.start()
        self.addCleanup(patch_prov.stop)

        # Create server with mock EscrowClient and stored credential paths
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            self.mcp = create_server(
                self.state_dir,
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
        self.assertEqual(proposal["amount"], 490_000)
        self.assertEqual(proposal["amount_usd"], "0.49")
        self.assertEqual(proposal["category"], DEFAULT_CATEGORY)
        self.assertTrue(proposal["task_id"].startswith("0x"))
        self.assertTrue(proposal["salt"].startswith("0x"))
        self.assertNotIn("policy_hash", proposal)
        self.assertIn("TaskEscrow.offer", proposal["instructions"])

    async def test_propose_deployment_calculates_real_hardware_pricing(self):
        # Specific GPU: L40 ($1.00/hr)
        p_l40 = await self.call("propose_deployment", {"hardware_id": 40})
        self.assertEqual(p_l40["amount"], 1_000_000)
        self.assertEqual(p_l40["amount_usd"], "1.00")
        self.assertEqual(p_l40["duration_hours"], 1)

        # Multi-hour duration: 2 hours on RTX A6000 ($0.49/hr -> $0.98 total)
        p2h = await self.call("propose_deployment", {"hardware_id": 21, "duration_hours": 2})
        self.assertEqual(p2h["amount"], 980_000)
        self.assertEqual(p2h["amount_usd"], "0.98")
        self.assertEqual(p2h["duration_hours"], 2)

    async def test_propose_deployment_enforces_budget_cap(self):
        # Budget cap lower than cheapest available ($0.49)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"budget_cap_usd": "0.40"})
        self.assertIn("exceeds budget cap", str(ctx.exception))

        # Budget cap lower than selected GPU ($1.00 > $0.80)
        with self.assertRaises(ToolError) as ctx:
            await self.call(
                "propose_deployment",
                {"hardware_id": 40, "budget_cap_usd": "0.80"},
            )
        self.assertIn("exceeds budget cap", str(ctx.exception))

        # Invalid duration
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"duration_hours": 0})
        self.assertIn("duration_hours must be at least 1", str(ctx.exception))

    async def test_propose_deployment_default_does_not_cap_below_manifest_budget(self):
        # Default manifest config/manifest.yaml allows up to $2.00; calling without budget_cap_usd
        # allows hardware costing $1.50 without being capped at $1.00.
        proposal = await self.call("propose_deployment", {"hardware_id": 30})
        self.assertEqual(proposal["amount_usd"], "1.50")
        self.assertEqual(proposal["amount"], 1_500_000)

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

        # 6. Check post-settlement status
        post_status = await self.call("get_deployment_status", {"task_id": task_id})
        self.assertEqual(post_status["status"], "settled")
        self.assertEqual(post_status["settlement_transaction"], "0x" + "aa" * 32)

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
            self.assertEqual(first_cmd[5], "warrant-worker")

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
            self.assertEqual(teardown_cmd, ["ssh", "petertower", "docker", "stop", "warrant-worker"])

    async def test_local_server_ssh_deployment_run_failure(self):
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            local_mcp = create_server(
                self.state_dir,
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
        self.assertNotEqual(st1["worker_token"], st2["worker_token"])

        # Default get_deployment_status without task_id routes to latest active session
        st_default = await self.call("get_deployment_status")
        self.assertEqual(st_default["task_id"], t2)
        self.assertEqual(st_default["status"], "ready")

        # Nonexistent task_id returns missing status
        st_missing = await self.call("get_deployment_status", {"task_id": "0xnonexistent"})
        self.assertEqual(st_missing["status"], "missing")
        self.assertIn("No deployment session found for task '0xnonexistent'", st_missing["message"])

    def test_create_server_and_app_with_config(self):
        config = ServerConfig(
            state_dir=self.state_dir,
            rpc_url="http://127.0.0.1:8545",
            escrow_address="0x" + "11" * 20,
            token_address="0x" + "22" * 20,
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

    async def test_seller_tools_registered(self):
        async with Client(self.mcp) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
        self.assertEqual(tools, SELLER_TOOLS)

    async def test_task_id_behaviour(self):
        with self.assertRaises(ToolError) as ctx:
            await self.call("deploy_with_escrow", {"task_id": "", "policy": {"version": 1}})
        self.assertIn("task_id is required", str(ctx.exception))

        with self.assertRaises(ToolError) as ctx:
            await self.call("submit_acceptance", {"task_id": "", "acceptance": {}, "signature": []})
        self.assertIn("task_id is required", str(ctx.exception))

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

    async def test_live_mode_enforces_manifest_policy(self):
        cloud = FakeCloud()

        async def fake_connected(operation):
            return await operation(cloud)

        with (
            patch.dict(os.environ, {"IO_NET_API_KEY": "test-dummy-key"}),
            patch("showcases.ionet_arc.seller_mcp_server.connected", side_effect=fake_connected),
            patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow),
        ):
            live = create_server(self.state_dir)
            with self.assertRaises(ToolError) as ctx:
                await live.call_tool(
                    "propose_deployment", {"hardware_id": 30, "duration_hours": 2, "budget_cap_usd": None}
                )
            self.assertIn("exceeds budget cap", str(ctx.exception))

            res = await live.call_tool(
                "propose_deployment", {"hardware_id": 21, "duration_hours": 1, "budget_cap_usd": None}
            )
        proposal = json.loads(res.content[0].text)
        self.assertEqual(proposal["amount"], 490_000)
        self.assertIn("get_credit_status", cloud.calls)

    def test_main_exits_with_status_1_when_chain_unknown(self):
        with (
            patch.object(seller_module, "run_server", side_effect=ChainDetectionError("cannot determine the chain")),
            patch("sys.argv", ["seller_mcp_server"]),
            self.assertLogs(seller_module.logger, level="ERROR") as cm,
            self.assertRaises(SystemExit) as ctx,
        ):
            seller_module.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(any("cannot determine the chain" in msg for msg in cm.output))

    def test_main_keeps_other_errors(self):
        with (
            patch.object(seller_module, "run_server", side_effect=RuntimeError("other failure")),
            patch("sys.argv", ["seller_mcp_server"]),
            self.assertRaises(RuntimeError),
        ):
            seller_module.main()

    def test_run_server_passes_log_level_to_uvicorn(self):
        config = ServerConfig(state_dir=self.state_dir)
        with patch.object(seller_module, "create_app"), patch.object(seller_module.uvicorn, "run") as mock_run:
            seller_module.run_server(config)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["log_level"], seller_module.logger.getEffectiveLevel())


class SellerPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Tests for seller policy, manifest limits, credits, and validation."""

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

        manifest = yaml.safe_load(DEFAULT_MANIFEST_PATH.read_text())
        manifest["chain_id"] = 5042
        manifest["max_budget_usd"] = 1.00
        self.manifest_path = Path(self.tmp.name) / "manifest.yaml"
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
            "vendor_credential_path": self.cred_path,
            "vendor_signature_path": self.sig_path,
            "manifest_path": self.manifest_path,
            "buyer_account": SAMPLE_BUYER,
        }
        kwargs.update(overrides)
        with patch("showcases.ionet_arc.seller_mcp_server.EscrowClient", return_value=self.mock_escrow):
            return create_server(self.state_dir, **kwargs)

    @staticmethod
    def service_of(server):
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

    def provision_ready(self, server=None):
        """Replace the io.net provisioning with an immediate ready container."""
        return patch.object(
            self.service_of(server or self.mcp),
            "_provision_container",
            return_value="https://worker.example.invalid",
        )

    def test_start_refused_when_rpc_chain_unreadable(self):
        self.mock_escrow.rpc.chain_id.side_effect = RuntimeError("connection refused")
        with self.assertRaises(ChainDetectionError) as ctx:
            self.build()
        self.assertIn("cannot determine the chain", str(ctx.exception))
        self.assertNotIn("connection refused", str(ctx.exception))

    async def test_respects_custom_manifest_budget_cap(self):
        custom_manifest = Path(self.tmp.name) / "custom_manifest.yaml"
        base_manifest = yaml.safe_load(self.manifest_path.read_text())
        base_manifest["max_budget_usd"] = 2.00
        custom_manifest.write_text(yaml.dump(base_manifest))
        mcp = self.build(manifest_path=custom_manifest)

        # H100 costs $1.50/hr, which exceeds $1.00 but is under the manifest cap of $2.00
        prop = await self.call("propose_deployment", {"hardware_id": 30}, server=mcp)
        self.assertEqual(prop["amount"], 1_500_000)
        self.assertEqual(prop["amount_usd"], "1.50")

        # Exceeding the manifest cap of 2.00 (e.g. caller passes cap 1.20)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"hardware_id": 30, "budget_cap_usd": 1.20}, server=mcp)
        self.assertIn("exceeds", str(ctx.exception))

    async def test_respects_custom_manifest_duration_hours(self):
        custom_manifest = Path(self.tmp.name) / "custom_duration_manifest.yaml"
        base_manifest = yaml.safe_load(self.manifest_path.read_text())
        base_manifest["duration_hours"] = 2
        base_manifest["max_budget_usd"] = 2.00
        custom_manifest.write_text(yaml.dump(base_manifest))
        mcp = self.build(manifest_path=custom_manifest)

        prop_default = await self.call("propose_deployment", {"hardware_id": 21}, server=mcp)
        self.assertEqual(prop_default["duration_hours"], 2)

        prop_custom = await self.call("propose_deployment", {"hardware_id": 21, "duration_hours": 1}, server=mcp)
        self.assertEqual(prop_custom["duration_hours"], 1)

    async def test_refuses_hardware_without_valid_price(self):
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
            self.cloud.hardware = [{**SAMPLE_HARDWARE[0], "price": price}]
            with self.subTest(price=price), self.assertRaises(ToolError) as ctx:
                await self.call("propose_deployment")
            if message:
                self.assertIn(message, str(ctx.exception))
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])

    # Credits

    async def test_low_credits_refuse_proposal(self):
        self.cloud.credit_result = credit_status_response(0.48)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment")
        self.assertIn("credit", str(ctx.exception).lower())
        self.assertEqual(list((self.state_dir / "proposals").iterdir()), [])
        self.mock_escrow.task_id_for.assert_not_called()

    async def test_sufficient_credits_allow_proposal(self):
        self.cloud.credit_result = credit_status_response(2.5)
        proposal = await self.call("propose_deployment")
        self.assertEqual(proposal["status"], "proposed")
        self.assertIn("get_credit_status", self.cloud.calls)
        self.assertTrue((self.state_dir / "proposals" / f"{proposal['task_id']}.json").exists())

    async def test_credits_exactly_at_floor_allow_proposal(self):
        self.cloud.credit_result = credit_status_response(0.49)
        proposal = await self.call("propose_deployment")
        self.assertEqual(proposal["status"], "proposed")

    async def test_unreadable_credits_refuse_proposal(self):
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

    async def test_courtesy_credits_do_not_count(self):
        # 1.98 credits of which 1.50 are courtesy: 0.48 spendable is below 0.49 cost.
        self.cloud.credit_result = credit_status_response(1.98, courtesy_balance=1.5)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment")
        self.assertIn("below 0.49 USD", str(ctx.exception))
        # Spendable exactly 0.49 after courtesy is allowed; 0.48 is refused.
        self.cloud.credit_result = credit_status_response(1.99, courtesy_balance=1.5)
        self.assertEqual((await self.call("propose_deployment"))["status"], "proposed")
        self.cloud.credit_result = credit_status_response(1.98, courtesy_balance=1.5)
        with self.assertRaises(ToolError):
            await self.call("propose_deployment")

    async def test_credit_check_scales_with_hardware_cost(self):
        # L40 costs $1.00/hr. With $0.99 credits it is refused; with $1.00 it succeeds.
        self.cloud.credit_result = credit_status_response(0.99)
        with self.assertRaises(ToolError) as ctx:
            await self.call("propose_deployment", {"hardware_id": 40})
        self.assertIn("below 1.00 USD", str(ctx.exception))

        self.cloud.credit_result = credit_status_response(1.00)
        prop = await self.call("propose_deployment", {"hardware_id": 40})
        self.assertEqual(prop["status"], "proposed")

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

    async def test_stored_proposal_outside_limits_is_not_accepted(self):
        proposal, policy = await self.offered_proposal()
        prop_path = self.state_dir / "proposals" / f"{proposal['task_id']}.json"
        stored = json.loads(prop_path.read_text())
        tampered = (
            {"amount": 2_000_000},
            {"duration_hours": 0},
            {"chain_id": 5042002},
            {"amount": "490000"},
        )
        for change in tampered:
            prop_path.write_text(json.dumps({**stored, **change}))
            with self.subTest(change=change), self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": proposal["task_id"], "policy": policy})
            self.assertIn("limits", str(ctx.exception))
        self.mock_escrow.accept.assert_not_called()

    async def test_check_terms_fails_when_service_limits_unconfigured(self):
        service = self.service_of(self.mcp)
        proposal = {
            "chain_id": 5042,
            "duration_hours": 1,
            "amount": 1_000_000,
        }
        orig_budget = service.max_budget_usd
        orig_duration = service.duration_hours
        try:
            service.max_budget_usd = None
            with self.assertRaises(ToolError) as ctx:
                service._check_proposal_terms(proposal)
            self.assertIn("max_budget_usd is not configured in config/manifest.yaml", str(ctx.exception))

            service.max_budget_usd = orig_budget
            service.duration_hours = None
            with self.assertRaises(ToolError) as ctx:
                service._check_proposal_terms(proposal)
            self.assertIn("duration_hours is not configured in config/manifest.yaml", str(ctx.exception))
        finally:
            service.max_budget_usd = orig_budget
            service.duration_hours = orig_duration

    async def test_propose_fails_when_service_limits_unconfigured(self):
        service = self.service_of(self.mcp)
        orig_budget = service.max_budget_usd
        orig_duration = service.duration_hours
        try:
            service.max_budget_usd = None
            with self.assertRaises(ToolError) as ctx:
                await service.propose_deployment()
            self.assertIn("max_budget_usd is not configured in config/manifest.yaml", str(ctx.exception))

            service.max_budget_usd = orig_budget
            service.duration_hours = None
            with self.assertRaises(ToolError) as ctx:
                await service.propose_deployment()
            self.assertIn("duration_hours is not configured in config/manifest.yaml", str(ctx.exception))
        finally:
            service.max_budget_usd = orig_budget
            service.duration_hours = orig_duration

    async def test_task_id_must_be_a_task_hash(self):
        for task_id in ("../active_session", "0x1234", "0x" + "zz" * 32):
            with self.subTest(task_id=task_id), self.assertRaises(ToolError) as ctx:
                await self.call("deploy_with_escrow", {"task_id": task_id, "policy": {"version": 1}})
            self.assertIn("32-byte hex", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
