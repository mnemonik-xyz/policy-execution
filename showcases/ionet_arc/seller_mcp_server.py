"""Streamable HTTP MCP server for Warrant GPU transcription on io.net CaaS."""

import argparse
import asyncio
import atexit
import base64
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.server import ThreadingHTTPServer

# Ensure repository root is on sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=getattr(logging, os.getenv("WARRANT_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.responses import Response  # noqa: E402

from showcases.ionet_arc.adapter import (  # noqa: E402
    PilotError,
    connected,
    deploy,
    read_json,
    save,
    validate_deployment,
)
from showcases.ionet_arc.escrow import (  # noqa: E402
    DEFAULT_CATEGORY,
    DEFAULT_CHAIN_ID,
    DEFAULT_MANIFEST_PATH,
    MAINNET_CHAIN_IDS,
    EscrowClient,
    address20,
    bytes32,
    evaluate_authorization,
    get_manifest_parameter,
    hash_policy,
    load_manifest,
    prove_authorization,
    read_private_key_file,
    should_trust_env,
)
from showcases.ionet_arc.worker import Worker, handler  # noqa: E402

PILOT_IMAGE = "ghcr.io/mnemonik-xyz/warrant-transcription:pilot"
SETTLEMENT_MODES = ("mock", "proof")


def resolve_settlement_mode(mode: str | None, chain_id: int | None) -> str:
    """Pick the settlement mode. Mainnet chains always use a real proof; the mock verifier is refused there."""
    if mode is not None and mode not in SETTLEMENT_MODES:
        raise ValueError(f"settlement must be one of {SETTLEMENT_MODES}, not {mode!r}")
    mainnet = chain_id is not None and int(chain_id) in MAINNET_CHAIN_IDS
    if mode is None:
        return "proof" if mainnet else "mock"
    if mode == "mock" and mainnet:
        raise ValueError(f"Mock settlement is refused on chain {chain_id}; set settlement: proof")
    return mode


class BasicAuthMiddleware:
    """Lightweight ASGI middleware enforcing HTTP Basic Authentication."""

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.expected_auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_val = headers.get(b"authorization", b"").decode("latin-1")
            if not secrets.compare_digest(auth_val, self.expected_auth):
                response = Response(
                    "Unauthorized",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="MCP"'},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def unwrap_error(exc: Exception) -> Exception | None:
    if isinstance(exc, (PilotError, FileExistsError, FileNotFoundError, json.JSONDecodeError, ValueError)):
        return exc
    if hasattr(exc, "exceptions"):
        for sub in getattr(exc, "exceptions", []):
            found = unwrap_error(sub)
            if found:
                return found
    return None


def safe_tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    pilot = unwrap_error(exc)
    if pilot:
        return ToolError(str(pilot))
    return ToolError(f"Operation failed: {exc!s}")


DEFAULT_MOCK_HARDWARE = [
    {
        "hardware_id": 3,
        "hardware_name": "GeForce RTX 3090",
        "price": 0.27,
        "available": 5,
        "max_gpus_per_container": 1,
        "location": "US",
    },
    {
        "hardware_id": 12,
        "hardware_name": "GeForce RTX 4090",
        "price": 0.30,
        "available": 100,
        "max_gpus_per_container": 1,
        "location": "US",
    },
    {
        "hardware_id": "gpu_1x_l40",
        "hardware_name": "L40",
        "price": 0.66,
        "available": 10,
        "max_gpus_per_container": 1,
        "location": "US",
    },
]


def load_cached_hardware() -> list[dict]:
    """Fallback hardware list when running offline or without live credentials."""
    for cand in [
        REPO_ROOT / "showcases" / "ionet_arc" / "mock_hardware.json",
        REPO_ROOT / "artifacts" / "io-hardware.json",
    ]:
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
                if isinstance(data, list):
                    return data
                res = data.get("result", {})
                if "data" in res and "data" in res["data"]:
                    return res["data"]["data"]
            except Exception:
                pass
    return DEFAULT_MOCK_HARDWARE


def _is_single_gpu(hw_id: str | int | None, max_gpus: int | None) -> bool:
    if max_gpus == 1:
        return True
    if isinstance(hw_id, str):
        lower = hw_id.lower()
        return "1x" in lower or "1-24g" in lower
    return bool(isinstance(hw_id, int))


def _is_whisper_friendly(hw_name: str, hw_id: str | int | None) -> bool:
    friendly_names = ["3090", "4090", "L40", "A6000", "A100", "H100", "T4", "V100", "A40", "A30", "L4", "ADA"]
    if any(g in hw_name for g in friendly_names):
        return True
    return isinstance(hw_id, int)


def filter_suitable_gpus(hardware_items: list[dict]) -> list[dict]:
    """Filter suitable single GPUs for faster-whisper-small inference, sorted by price."""
    suitable = []
    for item in hardware_items:
        hw_id = item.get("hardware_id")
        hw_name = str(item.get("hardware_name", "")).upper()
        available = item.get("available", 0)
        price = item.get("price")
        max_gpus = item.get("max_gpus_per_container")

        if not hw_id or available is None or available < 1 or price is None or price <= 0:
            continue
        if not _is_single_gpu(hw_id, max_gpus):
            continue
        if not _is_whisper_friendly(hw_name, hw_id):
            continue

        suitable.append(
            {
                "hardware_id": hw_id,
                "hardware_name": item.get("hardware_name", str(hw_id)),
                "price_per_hour_usd": float(price),
                "available_replicas": int(available),
                "location": item.get("location") or "US",
            }
        )

    suitable.sort(key=lambda x: x["price_per_hour_usd"])
    return suitable


class LocalMockWorker:
    """Mock in-process transcription worker for offline testing when IO_NET_API_KEY is unset."""

    def __init__(self, work_dir: pathlib.Path, token: str):
        self.work_dir = work_dir
        self.token = token
        self.server = None
        self.port = 0
        self.thread = None
        self.worker = None

    def start(self):
        def dummy_engine(audio, model_dir, language=None):
            return {
                "language": language or "en",
                "audio_seconds": 3.5,
                "segments": [
                    {"start": 0.0, "end": 1.5, "text": "Warrant autonomous transcription pilot."},
                    {"start": 1.5, "end": 3.5, "text": "io.net container compute verified on-chain."},
                ],
            }

        self.worker = Worker(self.work_dir, self.token, engine=dummy_engine)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.worker))
            self.port = self.server.server_port
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            return f"http://127.0.0.1:{self.port}"
        except OSError:
            self.port = 8080
            return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.worker:
            self.worker.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class WarrantTranscriptionService:
    """Encapsulates tool implementations and state for Warrant transcription server."""

    def __init__(
        self,
        state_dir: pathlib.Path,
        escrow_client: EscrowClient,
        use_mock_ionet: bool,
        local_server: bool = False,
        ssh_host: str = "localhost",
        worker_url: str | None = None,
        vendor_credential_path: pathlib.Path | None = None,
        vendor_signature_path: pathlib.Path | None = None,
        buyer_account: str | None = None,
        manifest_path: pathlib.Path | None = None,
        settlement_mode: str = "mock",
        worker_image: str = PILOT_IMAGE,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir = self.state_dir / "proposals"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.state_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.proofs_dir = self.state_dir / "proofs"
        if settlement_mode not in SETTLEMENT_MODES:
            raise ValueError(f"settlement_mode must be one of {SETTLEMENT_MODES}")
        self.settlement_mode = settlement_mode
        self.worker_image = worker_image
        self.escrow_client = escrow_client
        self.use_mock_ionet = use_mock_ionet
        self.local_server = local_server
        self.ssh_host = ssh_host
        self.worker_url = worker_url
        self.vendor_credential_path = pathlib.Path(vendor_credential_path) if vendor_credential_path else None
        self.vendor_signature_path = pathlib.Path(vendor_signature_path) if vendor_signature_path else None
        self.manifest_path = pathlib.Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH

        manifest = load_manifest(self.manifest_path)
        self.buyer_account = buyer_account or get_manifest_parameter(
            manifest, "buyer_account", "buyer-account", "buyer_address", "buyer-address"
        )
        if not self.escrow_client.escrow:
            self.escrow_client.escrow = get_manifest_parameter(manifest, "escrow_address", "escrow-address")
        if not self.escrow_client.token:
            self.escrow_client.token = get_manifest_parameter(manifest, "token_address", "token-address")
        if not self.escrow_client.verifier:
            self.escrow_client.verifier = get_manifest_parameter(manifest, "verifier_address", "verifier-address")
        if not self.escrow_client.server_account:
            self.escrow_client.server_account = get_manifest_parameter(
                manifest, "seller_address", "seller-address", "server_account", "server-account"
            )

        self.local_mock_workers: dict[str, LocalMockWorker] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        self._ssh_container_started = False
        if self.local_server:
            atexit.register(self._cleanup_ssh_container)
        atexit.register(self.stop)

    def stop(self):
        for worker in list(self.local_mock_workers.values()):
            worker.stop()
        self.local_mock_workers.clear()

    def _cleanup_ssh_container(self):
        if self.local_server and self._ssh_container_started:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["ssh", self.ssh_host, "docker", "stop", "warrant-transcription"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

    async def list_suitable_hardware(self) -> list[dict]:
        logger.info("Tool called: list_suitable_hardware (use_mock_ionet=%s)", self.use_mock_ionet)
        try:
            if self.use_mock_ionet:
                logger.info("Loading cached hardware configurations")
                raw_items = load_cached_hardware()
            else:
                logger.info("Calling io.net CaaS API to list available hardware")

                async def op(cloud):
                    res = await cloud.call("caas_get_hardware_ids", {})
                    data = res.get("data", {})
                    return data.get("data", []) if isinstance(data, dict) else []

                raw_items = await connected(op)

            logger.info("Filtering %d raw hardware options for Whisper transcription", len(raw_items))
            filtered = filter_suitable_gpus(raw_items)
            candidate_summary = [
                {"id": hw["hardware_id"], "name": hw["hardware_name"], "price_hr": hw["price_per_hour_usd"]}
                for hw in filtered
            ]
            logger.info("list_suitable_hardware returning %d suitable GPUs: %s", len(filtered), candidate_summary)
            return filtered
        except Exception as exc:
            logger.error("list_suitable_hardware failed: %s", exc)
            raise safe_tool_error(exc) from None

    def _select_hardware(self, candidates: list[dict], hardware_id: str | int | None) -> dict:
        if hardware_id is None:
            return candidates[0]
        for c in candidates:
            if str(c["hardware_id"]) == str(hardware_id):
                return c
        raise ToolError(f"Requested hardware_id '{hardware_id}' is not in available candidates.")

    async def _resolve_addresses_and_chain(self, customer_address: str | None) -> tuple[str, int]:
        escrow_client = self.escrow_client
        try:
            chain_id = await asyncio.to_thread(escrow_client.rpc.chain_id)
        except Exception:
            chain_id = DEFAULT_CHAIN_ID

        if not customer_address:
            customer_address = self.buyer_account

        if not customer_address:
            raise ToolError("Customer address was not provided and is not configured in config/manifest.yaml.")

        if not escrow_client.server_account:
            manifest = load_manifest(self.manifest_path)
            manifest_chain = manifest.get("chain_id")
            if manifest_chain is None or manifest_chain == chain_id:
                escrow_client.server_account = get_manifest_parameter(
                    manifest, "seller_address", "seller-address", "server_account", "server-account"
                )

        if not escrow_client.server_account:
            raise ToolError("Server payout account is not configured in config/manifest.yaml.")

        return customer_address, chain_id

    def _ensure_escrow_contracts(self, chain_id: int):
        escrow_client = self.escrow_client
        if not escrow_client.escrow or not escrow_client.token or not escrow_client.verifier:
            manifest = load_manifest(self.manifest_path)
            manifest_chain = manifest.get("chain_id")
            if manifest_chain is None or manifest_chain == chain_id:
                escrow_client.escrow = escrow_client.escrow or get_manifest_parameter(
                    manifest, "escrow_address", "escrow-address"
                )
                escrow_client.token = escrow_client.token or get_manifest_parameter(
                    manifest, "token_address", "token-address"
                )
                escrow_client.verifier = escrow_client.verifier or get_manifest_parameter(
                    manifest, "verifier_address", "verifier-address"
                )

        if not escrow_client.escrow:
            raise ToolError(
                f"TaskEscrow address is not configured for chain {chain_id}. "
                "Specify escrow_address in config/manifest.yaml."
            )
        if not escrow_client.token:
            raise ToolError(
                f"Payment token address is not configured for chain {chain_id}. "
                "Specify token_address in config/manifest.yaml."
            )

    async def propose_deployment(
        self,
        hardware_id: str | int | None = None,
        customer_address: str | None = None,
        location_id: str = "US",
        duration_hours: int = 1,
        budget_cap_usd: str | float | None = "1.00",
    ) -> dict:
        logger.info(
            "Tool called: propose_deployment with hardware_id=%s, customer_address=%s, location_id=%s, "
            "duration_hours=%s, budget_cap_usd=%s",
            hardware_id,
            customer_address,
            location_id,
            duration_hours,
            budget_cap_usd,
        )
        try:
            if duration_hours <= 0:
                logger.warning("Invalid duration_hours: %s", duration_hours)
                raise ToolError(f"duration_hours must be at least 1, got {duration_hours}")

            logger.info("Querying suitable hardware candidates for proposal...")
            candidates = await self.list_suitable_hardware()
            if not candidates:
                logger.warning("No suitable hardware candidates found")
                raise ToolError("No available suitable GPUs found.")

            selected_hw = self._select_hardware(candidates, hardware_id)
            logger.info(
                "Selected hardware '%s' (id=%s, price=$%s/hr, location=%s)",
                selected_hw["hardware_name"],
                selected_hw["hardware_id"],
                selected_hw["price_per_hour_usd"],
                selected_hw.get("location"),
            )
            customer_address, chain_id = await self._resolve_addresses_and_chain(customer_address)
            self._ensure_escrow_contracts(chain_id)

            salt = "0x" + secrets.token_hex(32)
            task_id = await asyncio.to_thread(self.escrow_client.task_id_for, customer_address, salt)
            logger.info("Derived taskIdFor(%s, salt=%s): %s", customer_address, salt, task_id)

            # Dynamic pricing based on real hardware cost and duration
            price_per_hour = Decimal(str(selected_hw["price_per_hour_usd"]))
            duration = Decimal(str(duration_hours))
            cost_usd = price_per_hour * duration

            if budget_cap_usd is not None:
                try:
                    cap = Decimal(str(budget_cap_usd))
                    if cost_usd > cap:
                        logger.warning("Hardware cost $%.2f exceeds budget cap $%.2f", cost_usd, cap)
                        raise ToolError(
                            f"Selected hardware '{selected_hw['hardware_name']}' cost ${cost_usd:.2f} "
                            f"exceeds budget cap of ${cap:.2f}."
                        )
                except (ValueError, InvalidOperation):
                    logger.warning("Invalid budget_cap_usd: %s", budget_cap_usd)
                    raise ToolError(f"Invalid budget_cap_usd: {budget_cap_usd}") from None

            token_amount = int(cost_usd.quantize(Decimal("0.000001")) * Decimal(1_000_000))
            amount_usd = (
                f"{cost_usd:.2f}"
                if cost_usd == cost_usd.quantize(Decimal("0.01"))
                else f"{cost_usd:.4f}".rstrip("0").rstrip(".")
            )

            proposal = {
                "status": "proposed",
                "task_id": task_id,
                "salt": salt,
                "customer": customer_address,
                "recipient": self.escrow_client.server_account,
                "escrow_address": self.escrow_client.escrow,
                "token_address": self.escrow_client.token,
                "verifier_address": self.escrow_client.verifier,
                "chain_id": chain_id,
                "policy_version": 1,
                "category": DEFAULT_CATEGORY,
                "amount": token_amount,
                "amount_usd": amount_usd,
                "hardware_id": selected_hw["hardware_id"],
                "hardware_name": selected_hw["hardware_name"],
                "price_per_hour_usd": selected_hw["price_per_hour_usd"],
                "location_ids": selected_hw.get("location") or location_id,
                "duration_hours": duration_hours,
                "billing_model": "duration",
                "image_url": self.worker_image,
                "traffic_port": 8080,
                "proposed_at": int(time.time()),
                "instructions": (
                    "Verify this proposal against your Warrant policy (max budget, category, duration).\n"
                    "Call TaskEscrow.offer(salt, recipient, policyHash, policyVersion, amount, "
                    "acceptBy, settleBy) on-chain.\n"
                    "Call deploy_with_escrow(task_id, policy) to commit agreement and begin provisioning.\n"
                    "Poll get_deployment_status until ready, verify container /health, and sign Acceptance evidence.\n"
                    "Submit acceptance via submit_acceptance to settle escrow on-chain, then call transcribe_audio."
                ),
            }

            prop_path = self.proposals_dir / f"{task_id}.json"
            save(prop_path, proposal)
            logger.info("Saved proposal record to %s", prop_path)
            logger.info(
                "propose_deployment returning: task_id=%s, amount=%d ($%s), recipient=%s, escrow=%s",
                task_id,
                token_amount,
                amount_usd,
                proposal["recipient"],
                proposal["escrow_address"],
            )
            return proposal
        except Exception as exc:
            logger.error("propose_deployment failed: %s", exc)
            raise safe_tool_error(exc) from None

    async def _verify_task_offered(self, task_id: str, proposal: dict) -> dict:
        try:
            task_data = await asyncio.to_thread(self.escrow_client.get_task, task_id)
        except Exception as e:
            raise ToolError(f"Failed to query TaskEscrow for task '{task_id}': {e}") from e

        task_state = task_data.get("state")
        if task_state == "Missing":
            raise ToolError(
                f"Task '{task_id}' has not been offered on TaskEscrow ({self.escrow_client.escrow}). "
                "Customer must submit TaskEscrow.offer() before deploying."
            )
        if task_state in ("Paid", "Refunded"):
            raise ToolError(f"Task '{task_id}' is already in terminal state: {task_state}.")

        # Enforce on-chain terms match proposal
        if task_data.get("amount") is not None and task_data.get("amount") != proposal.get("amount"):
            raise ToolError(
                f"On-chain amount ({task_data.get('amount')}) does not match proposal ({proposal.get('amount')})."
            )
        for key, label in (("recipient", "recipient"), ("customer", "customer")):
            if task_data.get(key) and proposal.get(key) and str(task_data[key]).lower() != str(proposal[key]).lower():
                raise ToolError(
                    f"On-chain {label} ({task_data.get(key)}) does not match proposal ({proposal.get(key)})."
                )
        now = int(time.time())
        min_settle_by = now + 60
        if task_data.get("settle_by") and task_data["settle_by"] < min_settle_by:
            raise ToolError(
                f"On-chain settleBy ({task_data.get('settle_by')}) does not provide sufficient time "
                f"(minimum {min_settle_by})."
            )

        return task_data

    async def _accept_task(self, task_id: str, proposal: dict, task_state: str | None = None):
        if task_state is None:
            task_data = await asyncio.to_thread(self.escrow_client.get_task, task_id)
            task_state = task_data.get("state")

        if task_state == "Offered":
            sender = proposal.get("recipient") or self.escrow_client.server_account
            try:
                await asyncio.to_thread(self.escrow_client.accept, task_id, sender=sender)
            except Exception as e:
                raise ToolError(f"Failed to accept TaskEscrow task '{task_id}' on-chain: {e}") from e

    async def _provision_container(self, proposal: dict, deployment_id: str, worker_token: str) -> str | None:
        if self.local_server:
            # 1. Clean up any existing container on SSH host
            await asyncio.to_thread(
                subprocess.run,
                ["ssh", self.ssh_host, "docker", "rm", "-f", "warrant-transcription"],
                capture_output=True,
                check=False,
            )

            # 2. Start container on SSH host
            run_cmd = [
                "ssh",
                self.ssh_host,
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                "warrant-transcription",
                "--gpus",
                "all",
                "-p",
                "8080:8080",
                "-e",
                f"WARRANT_WORKER_TOKEN={worker_token}",
                self.worker_image,
            ]
            run_res = await asyncio.to_thread(
                subprocess.run,
                run_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if run_res.returncode != 0:
                err_msg = run_res.stderr.strip() or run_res.stdout.strip()
                raise ToolError(f"Failed to start container on {self.ssh_host}: {err_msg}")

            self._ssh_container_started = True

            # 3. Poll container health endpoint until ready
            target_url = (self.worker_url or f"http://{self.ssh_host}:8080").rstrip("/")
            health_url = f"{target_url}/health"
            trust_env = should_trust_env(target_url)

            ready = False
            for _ in range(30):
                await asyncio.sleep(1)
                try:
                    async with httpx2.AsyncClient(timeout=3.0, trust_env=trust_env) as client:
                        resp = await client.get(health_url)
                        if resp.status_code == 200:
                            ready = True
                            break
                except Exception:
                    pass

            if not ready:
                raise ToolError(
                    f"Container on {self.ssh_host} failed to become healthy at {health_url} within 30 seconds."
                )

            return target_url

        if self.use_mock_ionet:
            task_id = proposal.get("task_id", deployment_id)
            if task_id in self.local_mock_workers:
                self.local_mock_workers[task_id].stop()
            worker_data = self.state_dir / "worker_data" / task_id
            mock_worker = LocalMockWorker(worker_data, worker_token)
            self.local_mock_workers[task_id] = mock_worker
            return mock_worker.start()

        duration = proposal.get("duration_hours", 1)
        deploy_args = {
            "billing_model": "duration",
            "hardware_id": proposal["hardware_id"],
            "location_ids": proposal["location_ids"],
            "duration_hours": duration,
            "gpus_per_container": 1,
            "replica_count": 1,
            "resource_private_name": f"warrant-whisper-{proposal['task_id'][-8:]}",
            "image_url": self.worker_image,
            "traffic_port": 8080,
            "env_variables": {"WARRANT_WORKER_TOKEN": worker_token},
        }
        validate_deployment(deploy_args)
        price_args = {
            "hardware_id": deploy_args["hardware_id"],
            "location_ids": deploy_args["location_ids"],
            "duration_hours": duration,
            "gpus_per_container": 1,
            "replica_count": 1,
        }
        state_file = self.state_dir / f"{deployment_id}.json"

        async def op(cloud):
            est = await cloud.call("caas_get_price_estimate", price_args)
            est_obj = {
                "tool": "caas_get_price_estimate",
                "observed_at": int(time.time()),
                "arguments": price_args,
                "result": est,
            }
            st = await deploy(
                cloud,
                deploy_args,
                state_file,
                est_obj,
                "/data/data/total_cost_usdc",
                proposal["amount_usd"],
            )
            dep_id = st.get("deployment_id")
            for _ in range(30):
                containers = await cloud.call("caas_get_deployment_containers", {"deployment_id": dep_id})
                workers = containers.get("data", {}).get("data", {}).get("workers", [])
                if workers and workers[0].get("public_url"):
                    return dep_id, workers[0]["public_url"]
                await asyncio.sleep(3)
            return dep_id, None

        _, public_url = await connected(op)
        return public_url

    async def _provision_container_bg(
        self,
        task_id: str,
        proposal: dict,
        deployment_id: str,
        worker_token: str,
    ):
        logger.info(
            "Starting background container provisioning for task_id=%s, deployment_id=%s",
            task_id,
            deployment_id,
        )
        try:
            public_url = await self._provision_container(proposal, deployment_id, worker_token)
            now = int(time.time())
            session = self._lookup_session(task_id) or {}
            session["status"] = "ready"
            session["public_url"] = public_url
            session["deployed_at"] = now
            session["expires_at"] = now + proposal.get("duration_hours", 1) * 3600
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)
            logger.info("Container provisioning complete for task_id=%s: public_url=%s", task_id, public_url)
        except Exception as exc:
            logger.error("Container provisioning failed for task_id=%s: %s", task_id, exc)
            session = self._lookup_session(task_id) or {}
            session["status"] = "failed"
            session["error"] = str(exc)
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)

    async def deploy_with_escrow(self, task_id: str, policy: dict | None = None) -> dict:
        logger.info("Tool called: deploy_with_escrow with task_id=%s", task_id)
        try:
            prop_path = self.proposals_dir / f"{task_id}.json"
            if not prop_path.exists():
                logger.warning("No proposal record found for task_id '%s' at %s", task_id, prop_path)
                raise ToolError(f"No proposal record found for task_id '{task_id}'. Call propose_deployment first.")

            proposal = read_json(prop_path)
            target_policy = policy or proposal.get("policy")
            if not target_policy:
                logger.warning("Buyer policy must be provided for task '%s'", task_id)
                raise ToolError(f"Buyer policy must be provided to deploy_with_escrow for task '{task_id}'.")

            # 1. Verify on-chain escrow state and commercial terms
            logger.info("Verifying on-chain TaskEscrow offer for task_id=%s", task_id)
            task_data = await self._verify_task_offered(task_id, proposal)

            # 2. Hash policy received from buyer and verify against on-chain policyHash
            logger.info("Verifying buyer policy hash against on-chain policyHash for task_id=%s", task_id)
            computed_policy_hash = await asyncio.to_thread(hash_policy, target_policy)
            onchain_policy_hash = str(task_data.get("policy_hash", ""))
            if onchain_policy_hash.lower() != computed_policy_hash.lower():
                logger.warning(
                    "Policy hash mismatch for task_id=%s: onchain=%s, computed=%s",
                    task_id,
                    onchain_policy_hash,
                    computed_policy_hash,
                )
                raise ToolError(
                    f"On-chain policyHash ({onchain_policy_hash}) does not match "
                    f"provided policy hash ({computed_policy_hash})."
                )

            proposal["policy"] = target_policy
            proposal["policy_hash"] = computed_policy_hash
            save(prop_path, proposal)

            now = int(time.time())

            # 3. Accept task on-chain first to commit agreement before spinning up hardware
            logger.info("Accepting task_id=%s on-chain in TaskEscrow", task_id)
            await self._accept_task(task_id, proposal, task_data.get("state"))

            # 3. Generate capability token and deliverable hash
            worker_token = secrets.token_hex(24)
            deliverable_hash = "0x" + hashlib.sha256(worker_token.encode("utf-8")).hexdigest()
            deployment_id = f"dep-{now}-{uuid.uuid4().hex[:8]}"
            logger.info(
                "Generated worker_token, deliverable_hash=%s, deployment_id=%s for task_id=%s",
                deliverable_hash,
                deployment_id,
                task_id,
            )

            # 4. Save active deployment session (provisioning)
            session = {
                "status": "provisioning",
                "task_id": task_id,
                "deployment_id": deployment_id,
                "worker_token": worker_token,
                "deliverable_hash": deliverable_hash,
                "hardware_id": proposal["hardware_id"],
                "recipient": proposal["recipient"],
                "amount": proposal["amount"],
                "chain_id": proposal["chain_id"],
                "escrow_address": proposal["escrow_address"],
                "token_address": proposal["token_address"],
                "policy_hash": proposal["policy_hash"],
                "policy": proposal.get("policy"),
                "duration_hours": proposal.get("duration_hours", 1),
                "started_at": now,
                "public_url": None,
                "settlement_tx": None,
                "error": None,
            }
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)

            # 5. Spawn background provisioning
            logger.info("Spawning background container provisioning for deployment_id=%s", deployment_id)
            bg_task = asyncio.create_task(self._provision_container_bg(task_id, proposal, deployment_id, worker_token))
            self._bg_tasks.add(bg_task)
            bg_task.add_done_callback(self._bg_tasks.discard)

            # Brief pause to allow instant local mock workers to complete
            await asyncio.sleep(0.02)

            curr = self._lookup_session(task_id) or session
            curr_status = curr.get("status", "provisioning")

            result = {
                "status": curr_status,
                "task_id": task_id,
                "deployment_id": deployment_id,
                "public_url": curr.get("public_url"),
                "deliverable_hash": deliverable_hash,
                "message": (
                    "Deployment initiated. Container provisioning in background.\n"
                    "Poll get_deployment_status(task_id) until status is 'ready'."
                    if curr_status == "provisioning"
                    else "Container is ready. Verify /health and submit signed acceptance."
                ),
            }
            logger.info(
                "deploy_with_escrow returning: status=%s, task_id=%s, deployment_id=%s, public_url=%s",
                result["status"],
                result["task_id"],
                result["deployment_id"],
                result["public_url"],
            )
            return result
        except Exception as exc:
            logger.error("deploy_with_escrow failed for task_id=%s: %s", task_id, exc)
            raise safe_tool_error(exc) from None

    @staticmethod
    def _transcription_result(job_id: str, job_status: dict) -> dict:
        res = job_status.get("result", {})
        segments = res.get("segments", [])
        return {
            "status": "complete",
            "job_id": job_id,
            "text": " ".join(s.get("text", "").strip() for s in segments),
            "language": res.get("language"),
            "audio_seconds": res.get("audio_seconds"),
            "segments": segments,
            "elapsed_seconds": job_status.get("elapsed_seconds"),
        }

    async def _poll_job_status(
        self,
        poll_fn,
        job_id: str,
        timeout_error: str = "Transcription timed out waiting for worker.",
    ) -> dict:
        for _ in range(120):  # poll up to 60 seconds
            try:
                job_status = poll_fn()
                if asyncio.iscoroutine(job_status):
                    job_status = await job_status
                if job_status:
                    st = job_status.get("status")
                    if st == "complete":
                        return self._transcription_result(job_id, job_status)
                    elif st in ("failed", "interrupted"):
                        err_msg = job_status.get("error") or st
                        raise ToolError(f"Transcription job failed: {err_msg}")
            except httpx2.HTTPError:
                pass
            await asyncio.sleep(0.5)

        raise ToolError(timeout_error)

    async def _poll_transcription(self, client: httpx2.AsyncClient, poll_url: str, headers: dict, job_id: str) -> dict:
        async def fetch():
            resp = await client.get(poll_url, headers=headers, timeout=10.0)
            return resp.json() if resp.status_code == 200 else None

        return await self._poll_job_status(fetch, job_id)

    def _find_latest_unexpired_session(self) -> dict | None:
        now = time.time()
        sessions_dir = self.state_dir / "sessions"
        if not sessions_dir.exists():
            return None
        candidates = []
        for p in sessions_dir.glob("*.json"):
            try:
                s = read_json(p)
                if s.get("expires_at", 0) > now and s.get("public_url"):
                    candidates.append(s)
            except Exception:
                pass
        if not candidates:
            return None
        candidates.sort(key=lambda s: s.get("deployed_at", 0), reverse=True)
        return candidates[0]

    def _lookup_session(self, task_id: str | None = None) -> dict | None:
        if task_id:
            session_file = self.state_dir / "sessions" / f"{task_id}.json"
            return read_json(session_file) if session_file.exists() else None
        session_file = self.state_dir / "active_session.json"
        if session_file.exists():
            candidate = read_json(session_file)
            if time.time() <= candidate.get("expires_at", 0):
                return candidate
        return self._find_latest_unexpired_session()

    def _active_session(self, task_id: str | None = None) -> dict:
        session = self._lookup_session(task_id)
        if session is None:
            if task_id:
                raise ToolError(f"No deployment session found for task '{task_id}'.")
            raise ToolError(
                "No active deployment session found. "
                "Prerequisites: list_suitable_hardware -> propose_deployment -> deploy_with_escrow."
            )

        if time.time() > session.get("expires_at", 0):
            target = f"for task '{task_id}' " if task_id else ""
            raise ToolError(
                f"Deployment session {target}expired at {session.get('expires_at')}. "
                "The 1-hour duration window has ended. A new proposal and deployment are required."
            )

        if not session.get("public_url"):
            raise ToolError("Deployment public URL is not ready yet.")
        return session

    async def transcribe_audio(
        self,
        audio_base64: str,
        language: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        logger.info(
            "Tool called: transcribe_audio with audio length=%d base64 chars, language=%s, task_id=%s",
            len(audio_base64) if audio_base64 else 0,
            language,
            task_id,
        )
        try:
            session = self._active_session(task_id)
            public_url = session["public_url"]
            worker_token = session.get("worker_token")
            logger.info("Found active session for task_id=%s: worker public_url=%s", session.get("task_id"), public_url)

            try:
                audio_bytes = base64.b64decode(audio_base64, validate=True)
            except ValueError as exc:
                logger.warning("Invalid base64 payload provided to transcribe_audio")
                raise ToolError("audio_base64 is not valid base64.") from exc
            if len(audio_bytes) == 0:
                logger.warning("Empty audio payload provided to transcribe_audio")
                raise ToolError("Audio file is empty.")

            worker_base = public_url.rstrip("/")
            audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
            query = f"?language={language.strip().lower()}" if language else ""
            req_url = f"{worker_base}/job{query}"

            headers = {
                "Authorization": f"Bearer {worker_token}",
                "Content-Type": "application/octet-stream",
                "X-Audio-SHA256": audio_sha256,
                "Content-Length": str(len(audio_bytes)),
            }

            trust_env = should_trust_env(worker_base)
            logger.info(
                "Submitting audio (%d bytes, sha256=%s) to worker endpoint %s",
                len(audio_bytes),
                audio_sha256,
                req_url,
            )

            try:
                async with httpx2.AsyncClient(timeout=30.0, trust_env=trust_env) as client:
                    resp = await client.post(req_url, content=audio_bytes, headers=headers)
                    if resp.is_error:
                        logger.error("Worker rejected upload HTTP %d: %s", resp.status_code, resp.text)
                        raise ToolError(f"Worker rejected upload with HTTP {resp.status_code}: {resp.text}")
                    resp_data = resp.json()
                    job_id = resp_data.get("job_id")
                    if not job_id:
                        logger.error("Unexpected worker response: %s", resp_data)
                        raise ToolError(f"Unexpected worker response: {resp_data}")

                    logger.info("Worker accepted job (job_id=%s). Polling for transcription completion...", job_id)
                    poll_url = f"{worker_base}/job/{job_id}"
                    poll_headers = {"Authorization": f"Bearer {worker_token}"}
                    result = await self._poll_transcription(client, poll_url, poll_headers, job_id)
                    text = result.get("text", "")
                    text_preview = (text[:80] + "...") if len(text) > 80 else text
                    logger.info(
                        "transcribe_audio returning: job_id=%s, status=%s, language=%s, "
                        "text_preview=%r, elapsed_seconds=%s",
                        result.get("job_id"),
                        result.get("status"),
                        result.get("language"),
                        text_preview,
                        result.get("elapsed_seconds"),
                    )
                    return result
            except (httpx2.HTTPError, OSError) as net_err:
                logger.error("Worker connection failed during transcribe_audio: %s", net_err)
                raise ToolError(f"Worker connection failed: {net_err}") from net_err
        except Exception as exc:
            logger.error("transcribe_audio failed: %s", exc)
            raise safe_tool_error(exc) from None

    async def get_deployment_status(self, task_id: str | None = None) -> dict:
        logger.info("Tool called: get_deployment_status with task_id=%s", task_id)
        session = self._lookup_session(task_id)
        if not session:
            msg = f"No deployment session found for task '{task_id}'." if task_id else "No deployment session active."
            logger.info("get_deployment_status returning: active=False, status=missing (%s)", msg)
            return {"active": False, "status": "missing", "message": msg}

        status = session.get("status", "ready")
        now = time.time()

        if status == "provisioning":
            elapsed = int(now - session.get("started_at", now))
            res = {
                "active": False,
                "status": "provisioning",
                "task_id": session.get("task_id"),
                "deployment_id": session.get("deployment_id"),
                "deliverable_hash": session.get("deliverable_hash"),
                "elapsed_seconds": elapsed,
                "message": f"Container is provisioning ({elapsed}s elapsed). Please continue polling.",
            }
            logger.info("get_deployment_status returning: status=provisioning, elapsed_seconds=%d", elapsed)
            return res

        if status == "failed":
            res = {
                "active": False,
                "status": "failed",
                "task_id": session.get("task_id"),
                "deployment_id": session.get("deployment_id"),
                "error": session.get("error"),
                "message": f"Container provisioning failed: {session.get('error')}",
            }
            logger.info("get_deployment_status returning: status=failed, error=%s", session.get("error"))
            return res

        remaining = max(0, int(session.get("expires_at", 0) - now))
        is_expired = remaining == 0
        res = {
            "active": not is_expired,
            "status": status,
            "task_id": session.get("task_id"),
            "deployment_id": session.get("deployment_id"),
            "public_url": session.get("public_url"),
            "worker_token": session.get("worker_token"),
            "deliverable_hash": session.get("deliverable_hash"),
            "hardware_id": session.get("hardware_id"),
            "remaining_seconds": remaining,
            "is_expired": is_expired,
            "settlement_transaction": session.get("settlement_tx"),
            "settlement": session.get("settlement"),
        }
        logger.info(
            "get_deployment_status returning: status=%s, active=%s, task_id=%s, remaining_seconds=%d, public_url=%s",
            res["status"],
            res["active"],
            res["task_id"],
            remaining,
            res["public_url"],
        )
        return res

    def _resolve_vendor_credential(self, session: dict) -> tuple[dict, list[int]]:
        """Resolve stored VendorCredential and its 64-byte signature matching session scope and recipient."""
        import yaml

        cred_path = self.vendor_credential_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / "config" / "vendor_credential.yaml"
        )
        sig_path = self.vendor_signature_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / "config" / "vendor_signature.yaml"
        )

        if not cred_path.exists():
            raise ToolError(f"Vendor credential file missing: {cred_path}")
        if not sig_path.exists():
            raise ToolError(f"Vendor signature file missing: {sig_path}")

        try:
            c_yaml = yaml.safe_load(cred_path.read_text())
        except Exception as e:
            raise ToolError(f"Failed to parse vendor credential at {cred_path}: {e}") from e

        try:
            s_yaml = yaml.safe_load(sig_path.read_text())
        except Exception as e:
            raise ToolError(f"Failed to parse vendor signature at {sig_path}: {e}") from e

        if not isinstance(c_yaml, dict):
            raise ToolError(f"Invalid vendor credential format in {cred_path}")
        if not isinstance(s_yaml, dict) or "signature" not in s_yaml:
            raise ToolError(f"Invalid vendor signature format in {sig_path}")

        now = int(time.time())
        scope = {
            "chain_id": int(session["chain_id"]),
            "vault": list(address20(session["escrow_address"])),
            "token": list(address20(session["token_address"])),
        }
        recipient = list(address20(session["recipient"]))

        c_scope = c_yaml.get("scope", {})
        if int(c_scope.get("chain_id", 0)) != scope["chain_id"]:
            raise ToolError(
                f"Vendor credential chain_id mismatch: expected {scope['chain_id']}, got {c_scope.get('chain_id')}"
            )
        if address20(c_scope.get("vault", "")) != address20(session["escrow_address"]):
            raise ToolError(
                f"Vendor credential vault mismatch: expected {session['escrow_address']}, got {c_scope.get('vault')}"
            )
        if address20(c_scope.get("token", "")) != address20(session["token_address"]):
            raise ToolError(
                f"Vendor credential token mismatch: expected {session['token_address']}, got {c_scope.get('token')}"
            )
        if address20(c_yaml.get("recipient", "")) != address20(session["recipient"]):
            raise ToolError(
                f"Vendor credential recipient mismatch: expected {session['recipient']}, got {c_yaml.get('recipient')}"
            )

        valid_after = int(c_yaml.get("valid_after", 0))
        valid_until = int(c_yaml.get("valid_until", 0))
        if now < valid_after:
            raise ToolError(f"Vendor credential is not yet valid (valid_after: {valid_after}, current: {now})")
        if now > valid_until:
            raise ToolError(f"Vendor credential has expired (valid_until: {valid_until}, current: {now})")

        raw_sig = s_yaml["signature"].replace("0x", "")
        sig_bytes = list(bytes.fromhex(raw_sig))
        if len(sig_bytes) != 64:
            raise ToolError(f"Vendor signature length mismatch: expected 64 bytes, got {len(sig_bytes)}")

        cred_obj = {
            "scope": scope,
            "recipient": recipient,
            "category": int(c_yaml.get("category", DEFAULT_CATEGORY)),
            "valid_after": valid_after,
            "valid_until": valid_until,
        }
        return cred_obj, sig_bytes

    def _save_session(self, task_id: str, session: dict) -> None:
        save(self.state_dir / "active_session.json", session)
        save(self.state_dir / "sessions" / f"{task_id}.json", session)

    @staticmethod
    def _proving_response(session: dict) -> dict:
        settlement = session.get("settlement") or {}
        elapsed = int(time.time() - settlement.get("started_at", time.time()))
        return {
            "status": "proving",
            "task_id": session.get("task_id"),
            "settlement": settlement,
            "elapsed_seconds": elapsed,
            "message": (
                f"A real proof is being generated ({elapsed}s elapsed). Settlement follows automatically. "
                "Poll get_deployment_status(task_id) until status is 'settled' or 'settlement_failed'."
            ),
        }

    def _start_proof_settlement(
        self,
        task_id: str,
        session: dict,
        policy: dict,
        request: dict,
        evidence: dict,
        journal_bytes: bytes,
        sender: str | None,
    ) -> dict:
        attempt = int(session.get("settlement", {}).get("attempt", 0)) + 1
        work_dir = self.proofs_dir / task_id / f"attempt-{attempt}"
        session["status"] = "proving"
        session["journal_hex"] = "0x" + journal_bytes.hex()
        session["settlement"] = {
            "mode": "proof",
            "state": "proving",
            "attempt": attempt,
            "started_at": int(time.time()),
            "proof_dir": str(work_dir),
            "error": None,
        }
        self._save_session(task_id, session)
        logger.info("Starting background proof for task_id=%s (attempt %d) in %s", task_id, attempt, work_dir)
        bg = asyncio.create_task(
            self._settle_with_proof_bg(task_id, policy, request, evidence, journal_bytes, work_dir, sender)
        )
        self._bg_tasks.add(bg)
        bg.add_done_callback(self._bg_tasks.discard)
        return self._proving_response(session)

    async def _settle_with_proof_bg(
        self,
        task_id: str,
        policy: dict,
        request: dict,
        evidence: dict,
        journal_bytes: bytes,
        work_dir: pathlib.Path,
        sender: str | None,
    ) -> None:
        """Prove, check the proof against the native journal and the deployed image ID, then settle."""
        try:
            proof = await asyncio.to_thread(prove_authorization, policy, request, evidence, work_dir)
            if proof["journal"].lower() != "0x" + journal_bytes.hex():
                raise RuntimeError("Proven journal differs from the native journal")
            deployed_image = await self.escrow_client.async_image_id()
            if proof["imageId"].lower() != deployed_image.lower():
                raise RuntimeError(
                    f"Proof image ID {proof['imageId']} differs from the escrow image ID {deployed_image}"
                )
            logger.info("Proof ready for task_id=%s; settling TaskEscrow with the real seal", task_id)
            receipt = await self.escrow_client.async_settle(proof["seal"], journal_bytes, sender=sender)
            settlement_tx = receipt.get("transactionHash")
            session = self._lookup_session(task_id) or {}
            session["status"] = "settled"
            session["settlement_tx"] = settlement_tx
            session["settlement"] = {
                **(session.get("settlement") or {}),
                "state": "settled",
                "transaction": settlement_tx,
                "image_id": proof["imageId"],
                "journal_digest": proof["journalDigest"],
                "finished_at": int(time.time()),
            }
            self._save_session(task_id, session)
            logger.info("TaskEscrow settled with a real proof for task_id=%s in tx %s", task_id, settlement_tx)
        except Exception as exc:
            logger.error("Proof settlement failed for task_id=%s: %s", task_id, exc)
            session = self._lookup_session(task_id) or {}
            session["status"] = "settlement_failed"
            session["settlement"] = {
                **(session.get("settlement") or {}),
                "state": "failed",
                "error": str(exc),
                "finished_at": int(time.time()),
            }
            self._save_session(task_id, session)

    async def submit_acceptance(
        self,
        task_id: str,
        acceptance: dict,
        signature: list[int] | str,
    ) -> dict:
        """Verify buyer acceptance evidence off-chain, then settle TaskEscrow on-chain.

        Mock mode approves the journal digest and settles at once. Proof mode starts a real proof in the
        background and returns ``proving``; a failed attempt can be resubmitted, because the contract
        rejects a second settlement of a paid task.
        """
        sig_len = len(signature) if isinstance(signature, (list, bytes)) else len(str(signature))
        logger.info("Tool called: submit_acceptance with task_id=%s, signature_length=%d", task_id, sig_len)
        try:
            session = self._lookup_session(task_id)
            if not session:
                logger.warning("No deployment session found for task '%s'", task_id)
                raise ToolError(f"No deployment session found for task '{task_id}'.")

            if session.get("status") == "settled" and session.get("settlement_tx"):
                res = {
                    "status": "settled",
                    "task_id": task_id,
                    "settlement_transaction": session.get("settlement_tx"),
                    "settlement": session.get("settlement"),
                    "message": "Task is already settled.",
                }
                logger.info("submit_acceptance returning: task already settled (tx=%s)", session.get("settlement_tx"))
                return res
            if session.get("status") == "proving":
                res = self._proving_response(session)
                logger.info("submit_acceptance returning: proof still in progress for task_id=%s", task_id)
                return res

            expected_deliv = session.get("deliverable_hash")
            acc_deliv = acceptance.get("deliverable_hash")
            acc_deliv_hex = "0x" + bytes(acc_deliv).hex() if isinstance(acc_deliv, list) else str(acc_deliv).lower()

            logger.info("Verifying deliverable hash match: expected=%s, acceptance=%s", expected_deliv, acc_deliv_hex)
            if expected_deliv and acc_deliv_hex != expected_deliv.lower():
                logger.warning("Deliverable hash mismatch: provided %s != worker %s", acc_deliv_hex, expected_deliv)
                raise ToolError(
                    f"Deliverable hash mismatch: acceptance provided {acc_deliv_hex} "
                    f"but worker generated {expected_deliv}"
                )

            # Resolve policy
            policy = session.get("policy")
            if not policy:
                logger.warning("No policy associated with deployment session for task '%s'", task_id)
                raise ToolError(f"No policy associated with deployment session for task '{task_id}'.")

            # Build Request struct
            request = {
                "scope": {
                    "chain_id": int(session["chain_id"]),
                    "vault": list(address20(session["escrow_address"])),
                    "token": list(address20(session["token_address"])),
                },
                "task_id": list(bytes32(task_id)),
                "deliverable_hash": list(bytes32(session["deliverable_hash"])),
                "recipient": list(address20(session["recipient"])),
                "amount": int(session["amount"]),
            }

            # Convert signature if passed as hex string
            if isinstance(signature, str):
                raw_sig = signature.replace("0x", "")
                acc_sig_bytes = list(bytes.fromhex(raw_sig))
            else:
                acc_sig_bytes = list(signature)

            # Resolve VendorCredential and sign
            logger.info("Resolving vendor credential and signature for task_id=%s", task_id)
            v_cred, v_sig = self._resolve_vendor_credential(session)

            evidence = {
                "vendor": v_cred,
                "vendor_signature": v_sig,
                "acceptance": acceptance,
                "acceptance_signature": acc_sig_bytes,
            }

            # Execute real native off-chain policy evaluation (<10ms)
            logger.info("Evaluating off-chain Warrant authorization policy for task_id=%s", task_id)
            try:
                _auth, journal_bytes = await asyncio.to_thread(evaluate_authorization, policy, request, evidence)
                logger.info("Off-chain authorization verified successfully (journal=%d bytes)", len(journal_bytes))
            except Exception as e:
                logger.error("Off-chain Warrant authorization evaluation failed: %s", e)
                raise ToolError(f"Off-chain Warrant authorization evaluation failed: {e}") from e

            sender = session.get("recipient") or self.escrow_client.server_account
            if self.settlement_mode == "proof":
                # Real proof: prove in the background, then TaskEscrow.settle(seal, journal). Minutes, not seconds.
                return self._start_proof_settlement(task_id, session, policy, request, evidence, journal_bytes, sender)

            # Mock settlement (local chains and the testnet showcase): JournalVerifier approves the digest,
            # then settle with the mock seal. settle_mock itself refuses mainnet chains.
            settlement_tx = None
            try:
                logger.info("Settling TaskEscrow on-chain via settle_mock with journal digest")
                settle_res = await asyncio.to_thread(self.escrow_client.settle_mock, journal_bytes, sender=sender)
                settlement_tx = settle_res.get("transactionHash")
                logger.info("TaskEscrow settled on-chain in tx %s", settlement_tx)
            except Exception as err:
                logger.error("Escrow settlement failed on-chain: %s", err)
                raise ToolError(f"Escrow settlement failed on-chain: {err}") from err

            session["status"] = "settled"
            session["settlement_tx"] = settlement_tx
            session["journal_hex"] = "0x" + journal_bytes.hex()
            session["settlement"] = {"mode": "mock", "state": "settled", "transaction": settlement_tx}
            self._save_session(task_id, session)

            res = {
                "status": "settled",
                "task_id": task_id,
                "settlement_transaction": settlement_tx,
                "settlement": session["settlement"],
                "message": "Authorization successfully verified off-chain and TaskEscrow settled on-chain.",
            }
            logger.info(
                "submit_acceptance returning: status=%s, task_id=%s, tx=%s",
                res["status"],
                res["task_id"],
                settlement_tx,
            )
            return res
        except Exception as exc:
            logger.error("submit_acceptance failed: %s", exc)
            raise safe_tool_error(exc) from None


@dataclass
class ServerConfig:
    state_dir: pathlib.Path = pathlib.Path("artifacts/state")
    host: str = "localhost"
    port: int = 8000
    auth_user: str | None = None
    auth_pass: str | None = None
    rpc_url: str | None = None
    escrow_address: str | None = None
    token_address: str | None = None
    verifier_address: str | None = None
    server_account: str | None = None
    seller_private_key: str | None = None
    seller_key_path: pathlib.Path | None = None
    buyer_account: str | None = None
    settlement_mode: str | None = None
    worker_image: str | None = None
    chain_id: int | None = None
    mock_ionet: bool = False
    local_server: bool = False
    ssh_host: str = "localhost"
    worker_url: str | None = None
    vendor_credential_path: pathlib.Path | None = None
    vendor_signature_path: pathlib.Path | None = None
    manifest_path: pathlib.Path | None = None
    escrow_client: EscrowClient | None = None

    def _resolve_paths(self):
        if isinstance(self.state_dir, str):
            self.state_dir = pathlib.Path(self.state_dir)
        if isinstance(self.vendor_credential_path, str):
            self.vendor_credential_path = pathlib.Path(self.vendor_credential_path)
        if isinstance(self.vendor_signature_path, str):
            self.vendor_signature_path = pathlib.Path(self.vendor_signature_path)
        if isinstance(self.manifest_path, str):
            self.manifest_path = pathlib.Path(self.manifest_path)
        if isinstance(self.seller_key_path, str):
            self.seller_key_path = pathlib.Path(self.seller_key_path)

    def _resolve_parameters(self, manifest: dict):
        if self.rpc_url is None:
            self.rpc_url = get_manifest_parameter(manifest, "rpc_url", "rpc-url") or "http://127.0.0.1:8545"
        if self.escrow_address is None:
            self.escrow_address = get_manifest_parameter(manifest, "escrow_address", "escrow-address")
        if self.verifier_address is None:
            self.verifier_address = get_manifest_parameter(manifest, "verifier_address", "verifier-address")
        if self.buyer_account is None:
            self.buyer_account = get_manifest_parameter(
                manifest, "buyer_account", "buyer-account", "buyer_address", "buyer-address"
            )
        if self.token_address is None:
            self.token_address = get_manifest_parameter(manifest, "token_address", "token-address")
        if self.server_account is None:
            self.server_account = get_manifest_parameter(
                manifest, "seller_address", "seller-address", "server_account", "server-account"
            )
        if self.chain_id is None:
            raw_chain = get_manifest_parameter(manifest, "chain_id", "chain-id")
            self.chain_id = int(raw_chain) if raw_chain is not None else None
        manifest_mode = get_manifest_parameter(manifest, "settlement", "settlement_mode", "settlement-mode")
        self.settlement_mode = resolve_settlement_mode(self.settlement_mode or manifest_mode, self.chain_id)
        if self.worker_image is None:
            self.worker_image = get_manifest_parameter(manifest, "worker_image", "worker-image") or PILOT_IMAGE

    def _resolve_key(self, manifest: dict):
        if self.seller_key_path is None:
            raw_key_path = manifest.get("private_key_files", {}).get(
                "seller_key", manifest.get("private_key_files", {}).get("seller_account_key")
            )
            if raw_key_path:
                cand = pathlib.Path(raw_key_path)
                self.seller_key_path = cand if cand.is_absolute() else (REPO_ROOT / cand)
            else:
                default_seller_key = REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "seller.key"
                if default_seller_key.exists():
                    self.seller_key_path = default_seller_key

        if self.seller_private_key is None:
            if self.seller_key_path and self.seller_key_path.exists():
                # A key file that exists but is unreadable, shared or malformed is a configuration error.
                self.seller_private_key = read_private_key_file(self.seller_key_path)
            if not self.seller_private_key:
                for env_var in ("WARRANT_SELLER_KEY", "SELLER_PRIVATE_KEY", "WARRANT_PRIVATE_KEY"):
                    if os.environ.get(env_var):
                        self.seller_private_key = os.environ.get(env_var).strip()
                        break

    def __post_init__(self):
        self._resolve_paths()
        manifest = load_manifest(self.manifest_path)
        self._resolve_parameters(manifest)
        self._resolve_key(manifest)


SellerServerConfig = ServerConfig


def _normalize_config(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
) -> ServerConfig:
    if isinstance(config, ServerConfig):
        if kwargs:
            return ServerConfig(**{**config.__dict__, **kwargs})
        return config
    if isinstance(config, (pathlib.Path, str)):
        kwargs["state_dir"] = pathlib.Path(config)
    return ServerConfig(**kwargs)


def create_server(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
) -> MCPServer:
    """Create and configure the Warrant Transcription MCP server."""
    cfg = _normalize_config(config, **kwargs)
    server = MCPServer("seller-mcp-server")
    has_ionet_key = bool(os.environ.get("IO_NET_API_KEY"))
    use_mock_ionet = cfg.mock_ionet or not has_ionet_key

    escrow_client = cfg.escrow_client or EscrowClient(
        rpc_url=cfg.rpc_url,
        escrow_address=cfg.escrow_address,
        token_address=cfg.token_address,
        verifier_address=cfg.verifier_address,
        server_account=cfg.server_account,
    )
    if cfg.seller_private_key:
        if cfg.server_account:
            escrow_client.register_key(cfg.server_account, cfg.seller_private_key)
        with contextlib.suppress(Exception):
            from eth_account import Account
            derived_addr = Account.from_key(cfg.seller_private_key).address
            escrow_client.register_key(derived_addr, cfg.seller_private_key)

    service = WarrantTranscriptionService(
        state_dir=cfg.state_dir,
        escrow_client=escrow_client,
        use_mock_ionet=use_mock_ionet,
        local_server=cfg.local_server,
        ssh_host=cfg.ssh_host,
        worker_url=cfg.worker_url,
        vendor_credential_path=cfg.vendor_credential_path,
        vendor_signature_path=cfg.vendor_signature_path,
        buyer_account=cfg.buyer_account,
        manifest_path=cfg.manifest_path,
        settlement_mode=cfg.settlement_mode or "mock",
        worker_image=cfg.worker_image or PILOT_IMAGE,
    )
    logger.info("Settlement mode: %s; worker image: %s", service.settlement_mode, service.worker_image)

    server.tool(
        name="list_suitable_hardware",
        description=(
            "List available io.net GPU hardware options suitable for faster-whisper-small transcription.\n"
            "Filters for single-GPU configurations with live availability and sorts by price ascending."
        ),
    )(service.list_suitable_hardware)

    server.tool(
        name="propose_deployment",
        description=(
            "Generate an escrow proposal to deploy the transcription container for 1 hour duration.\n"
            "Selects the cheapest suitable available GPU (or a specific hardware_id), calculates pricing, "
            "and returns concrete TaskEscrow payment terms (taskId, salt, amount, recipient)."
        ),
    )(service.propose_deployment)

    server.tool(
        name="deploy_with_escrow",
        description=(
            "Verify on-chain escrow funding for the proposed task, accept it on-chain, and provision "
            "the io.net container in the background for 1 hour duration.\n"
            "Prerequisite: propose_deployment must have been called and TaskEscrow.offer executed on-chain."
        ),
    )(service.deploy_with_escrow)

    server.tool(
        name="transcribe_audio",
        description=(
            "Transcribe audio (passed as base64-encoded bytes in audio_base64) using an active "
            "1-hour Whisper deployment on io.net.\n"
            "Prerequisite: deploy_with_escrow must have been completed and the 1-hour window must not be expired.\n"
            "Optionally provide task_id to target a specific deployment when multiple tasks exist."
        ),
    )(service.transcribe_audio)

    server.tool(
        name="get_deployment_status",
        description=(
            "Check status of active 1-hour transcription deployment and remaining duration.\n"
            "Optionally provide task_id to inspect a specific deployment session."
        ),
    )(service.get_deployment_status)

    server.tool(
        name="submit_acceptance",
        description=(
            "Submit buyer-signed Acceptance evidence for a verified deliverable.\n"
            "Evaluates policy and signatures off-chain via Warrant and settles payment on TaskEscrow on-chain. "
            "In proof mode a real RISC Zero proof is generated first; the call returns 'proving' and "
            "get_deployment_status reports 'settled' or 'settlement_failed' later."
        ),
    )(service.submit_acceptance)

    return server


def create_app(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Build ASGI app with streamable HTTP transport and basic auth."""
    cfg = _normalize_config(config, **kwargs)
    server = create_server(cfg)
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(transport_security=sec)

    if cfg.auth_user and cfg.auth_pass:
        print(f"HTTP Basic Auth enabled for user '{cfg.auth_user}'", file=sys.stderr)
        app = BasicAuthMiddleware(app, cfg.auth_user, cfg.auth_pass)
    return app


def run_server(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Run MCP server with uvicorn."""
    cfg = _normalize_config(config, **kwargs)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


def main():
    parser = argparse.ArgumentParser(description="Run the Warrant GPU transcription MCP server over Streamable HTTP.")
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "localhost"),
        help="Host address to bind (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port number to bind (default: 8000)",
    )
    parser.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("IONET_STATE_DIR", "artifacts/state")),
        help="Directory to persist state journals (default: artifacts/state)",
    )
    parser.add_argument(
        "--auth-user",
        default=os.environ.get("MCP_AUTH_USER"),
        help="HTTP Basic Auth username (or set MCP_AUTH_USER)",
    )
    parser.add_argument(
        "--auth-pass",
        default=os.environ.get("MCP_AUTH_PASS"),
        help="HTTP Basic Auth password (or set MCP_AUTH_PASS)",
    )
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("WARRANT_RPC_URL", os.environ.get("ARC_RPC_URL")),
        help="Ethereum / Arc JSON-RPC URL (default: read from manifest.yaml or http://127.0.0.1:8545)",
    )
    parser.add_argument(
        "--manifest",
        dest="manifest_path",
        type=pathlib.Path,
        default=os.environ.get("WARRANT_MANIFEST"),
        help="Path to manifest.yaml config (default: showcases/ionet_arc/config/manifest.yaml)",
    )
    parser.add_argument(
        "--server-account",
        default=os.environ.get("WARRANT_SERVER_ACCOUNT"),
        help="Server recipient / agent account address",
    )
    parser.add_argument(
        "--mock-ionet",
        action="store_true",
        default=os.environ.get("MOCK_IONET", "").lower() in ("1", "true"),
        help="Run using local mock worker instead of live io.net CaaS",
    )
    parser.add_argument(
        "--local-server",
        dest="local_server",
        action="store_true",
        default=os.environ.get("LOCAL_SERVER", "").lower() in ("1", "true"),
        help="Deploy container to local/remote server via SSH instead of live io.net CaaS",
    )
    parser.add_argument(
        "--ssh-host",
        default=os.environ.get("SSH_HOST", "localhost"),
        help="SSH host target for container deployment (default: localhost)",
    )
    parser.add_argument(
        "--worker-url",
        default=os.environ.get("WORKER_URL"),
        help="HTTP URL for the deployed worker (default: http://<ssh-host>:8080)",
    )
    parser.add_argument(
        "--vendor-credential",
        dest="vendor_credential_path",
        type=pathlib.Path,
        default=os.environ.get("WARRANT_VENDOR_CREDENTIAL"),
        help="Path to vendor_credential.yaml (default: showcases/ionet_arc/config/vendor_credential.yaml)",
    )
    parser.add_argument(
        "--vendor-signature",
        dest="vendor_signature_path",
        type=pathlib.Path,
        default=os.environ.get("WARRANT_VENDOR_SIGNATURE"),
        help="Path to vendor_signature.yaml (default: showcases/ionet_arc/config/vendor_signature.yaml)",
    )
    parser.add_argument(
        "--key-file",
        dest="seller_key_path",
        type=pathlib.Path,
        default=None,
        help=(
            "Path to the seller private key file, mode 0600 (default: from manifest or .keys/seller.key). "
            "A key is never accepted on the command line."
        ),
    )
    parser.add_argument(
        "--settlement",
        dest="settlement_mode",
        choices=SETTLEMENT_MODES,
        default=os.environ.get("WARRANT_SETTLEMENT"),
        help="mock: JournalVerifier digest approval (local and testnet only); proof: real RISC Zero proof. "
        "Mainnet chains always use proof.",
    )
    args = parser.parse_args()

    config = ServerConfig(**vars(args))
    run_server(config)


if __name__ == "__main__":
    main()
