"""Streamable HTTP MCP server for Warrant GPU transcription on io.net CaaS."""

import argparse
import asyncio
import atexit
import base64
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from decimal import Decimal, InvalidOperation
from http.server import ThreadingHTTPServer

# Ensure repository root is on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import contextlib

import httpx2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response

from showcases.ionet_arc.adapter import (
    PilotError,
    canonical,
    connected,
    deploy,
    read_json,
    save,
    validate_deployment,
)
from showcases.ionet_arc.escrow import (
    DEFAULT_CATEGORY,
    DEFAULT_CHAIN_ID,
    REPO_ROOT,
    EscrowClient,
    encode_journal,
    ensure_policy_hash,
)
from showcases.ionet_arc.worker import Worker, handler

PILOT_IMAGE = "ghcr.io/mnemonik-xyz/warrant-transcription:pilot"


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
                res = data.get("result", {})
                if "data" in res and "data" in res["data"]:
                    return res["data"]["data"]
                if isinstance(data, list):
                    return data
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
        ssh_host: str = "petertower",
        worker_url: str | None = None,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir = self.state_dir / "proposals"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.state_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.escrow_client = escrow_client
        self.use_mock_ionet = use_mock_ionet
        self.local_server = local_server
        self.ssh_host = ssh_host
        self.worker_url = worker_url
        self.local_mock_workers: dict[str, LocalMockWorker] = {}
        self.active_local_worker = None
        self._ssh_container_started = False
        if self.local_server:
            atexit.register(self._cleanup_ssh_container)
        atexit.register(self.stop)

    def stop(self):
        for worker in list(self.local_mock_workers.values()):
            worker.stop()
        self.local_mock_workers.clear()
        self.active_local_worker = None

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
        try:
            if self.use_mock_ionet:
                raw_items = load_cached_hardware()
            else:

                async def op(cloud):
                    res = await cloud.call("caas_get_hardware_ids", {})
                    data = res.get("data", {})
                    return data.get("data", []) if isinstance(data, dict) else []

                raw_items = await connected(op)

            filtered = filter_suitable_gpus(raw_items)
            return filtered
        except Exception as exc:
            raise safe_tool_error(exc) from None

    def _select_hardware(self, candidates: list[dict], hardware_id: str | int | None) -> dict:
        if hardware_id is None:
            return candidates[0]
        for c in candidates:
            if str(c["hardware_id"]) == str(hardware_id):
                return c
        raise ToolError(f"Requested hardware_id '{hardware_id}' is not in available candidates.")

    def _resolve_addresses_and_chain(self, customer_address: str | None) -> tuple[str, int]:
        escrow_client = self.escrow_client
        if not customer_address:
            try:
                accounts = escrow_client.rpc.accounts()
                customer_address = accounts[0] if accounts else "0x" + "11" * 20
            except Exception:
                customer_address = "0x" + "11" * 20

        if not escrow_client.server_account:
            try:
                accounts = escrow_client.rpc.accounts()
                escrow_client.server_account = accounts[1] if len(accounts) > 1 else accounts[0]
            except Exception:
                escrow_client.server_account = "0x" + "22" * 20

        try:
            chain_id = escrow_client.rpc.chain_id()
        except Exception:
            chain_id = DEFAULT_CHAIN_ID

        return customer_address, chain_id

    def _ensure_escrow_contracts(self, customer_address: str):
        escrow_client = self.escrow_client
        if not escrow_client.escrow:
            try:
                dep = escrow_client.deploy_local_anvil(customer_address, escrow_client.server_account)
                print(f"Auto-deployed TaskEscrow on Anvil: {dep['escrow']}", file=sys.stderr)
            except Exception as err:
                print(
                    f"Warning: Could not auto-deploy on local anvil ({err}); using dummy addresses",
                    file=sys.stderr,
                )
                escrow_client.escrow = "0x" + "33" * 20
                escrow_client.token = "0x" + "44" * 20
                escrow_client.verifier = "0x" + "55" * 20

    async def propose_deployment(
        self,
        hardware_id: str | int | None = None,
        customer_address: str | None = None,
        location_id: str = "US",
        duration_hours: int = 1,
        budget_cap_usd: str | float | None = "1.00",
    ) -> dict:
        try:
            if duration_hours <= 0:
                raise ToolError(f"duration_hours must be at least 1, got {duration_hours}")

            candidates = await self.list_suitable_hardware()
            if not candidates:
                raise ToolError("No available suitable GPUs found.")

            selected_hw = self._select_hardware(candidates, hardware_id)
            customer_address, chain_id = self._resolve_addresses_and_chain(customer_address)
            self._ensure_escrow_contracts(customer_address)

            salt = "0x" + secrets.token_hex(32)
            task_id = self.escrow_client.task_id_for(customer_address, salt)

            policy_hash = ensure_policy_hash(
                self.escrow_client.escrow,
                self.escrow_client.token,
                chain_id,
                100_000_000,
                self.state_dir,
            )

            # Dynamic pricing based on real hardware cost and duration
            price_per_hour = Decimal(str(selected_hw["price_per_hour_usd"]))
            duration = Decimal(str(duration_hours))
            cost_usd = price_per_hour * duration

            if budget_cap_usd is not None:
                try:
                    cap = Decimal(str(budget_cap_usd))
                    if cost_usd > cap:
                        raise ToolError(
                            f"Selected hardware '{selected_hw['hardware_name']}' cost ${cost_usd:.2f} "
                            f"exceeds budget cap of ${cap:.2f}."
                        )
                except (ValueError, InvalidOperation):
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
                "policy_hash": policy_hash,
                "policy_version": 1,
                "category": DEFAULT_CATEGORY,
                "amount": token_amount,
                "amount_usd": amount_usd,
                "hardware_id": selected_hw["hardware_id"],
                "hardware_name": selected_hw["hardware_name"],
                "price_per_hour_usd": selected_hw["price_per_hour_usd"],
                "location_ids": location_id,
                "duration_hours": duration_hours,
                "billing_model": "duration",
                "image_url": PILOT_IMAGE,
                "traffic_port": 8080,
                "proposed_at": int(time.time()),
                "instructions": (
                    "Verify this proposal against your Warrant policy (max budget, category, duration).\n"
                    "Then call TaskEscrow.offer(salt, recipient, policyHash, policyVersion, amount, "
                    "acceptBy, settleBy) on-chain.\n"
                    "Once confirmed on-chain, call deploy_with_escrow(task_id)."
                ),
            }

            prop_path = self.proposals_dir / f"{task_id}.json"
            save(prop_path, proposal)
            return proposal
        except Exception as exc:
            raise safe_tool_error(exc) from None

    def _verify_task_offered(self, task_id: str, proposal: dict) -> dict:
        try:
            task_data = self.escrow_client.get_task(task_id)
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
        for key, label in (("recipient", "recipient"), ("policy_hash", "policyHash"), ("customer", "customer")):
            if task_data.get(key) and str(task_data[key]).lower() != str(proposal.get(key, "")).lower():
                raise ToolError(
                    f"On-chain {label} ({task_data.get(key)}) does not match proposal ({proposal.get(key)})."
                )
        now = int(time.time())
        min_settle_by = now + proposal.get("duration_hours", 1) * 3600
        if task_data.get("settle_by") and task_data["settle_by"] < min_settle_by:
            raise ToolError(
                f"On-chain settleBy ({task_data.get('settle_by')}) does not cover required duration "
                f"(minimum {min_settle_by})."
            )

        return task_data

    def _accept_task(self, task_id: str, proposal: dict, task_state: str | None = None):
        if task_state is None:
            task_data = self.escrow_client.get_task(task_id)
            task_state = task_data.get("state")

        if task_state == "Offered":
            sender = proposal.get("recipient") or self.escrow_client.server_account
            try:
                self.escrow_client.accept(task_id, sender=sender)
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
                PILOT_IMAGE,
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
            req_parsed = urllib.parse.urlsplit(target_url)
            trust_env = req_parsed.hostname not in ("127.0.0.1", "localhost", "::1")

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
            self.active_local_worker = mock_worker
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
            "image_url": PILOT_IMAGE,
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

    async def deploy_with_escrow(self, task_id: str) -> dict:
        try:
            prop_path = self.proposals_dir / f"{task_id}.json"
            if not prop_path.exists():
                raise ToolError(f"No proposal record found for task_id '{task_id}'. Call propose_deployment first.")

            proposal = read_json(prop_path)
            now = int(time.time())

            # 1. Verify on-chain escrow state and terms (do not accept yet to prevent capital lockup on failure)
            task_data = self._verify_task_offered(task_id, proposal)

            # 2. Provision container (1h duration)
            worker_token = secrets.token_hex(24)
            deployment_id = f"dep-{now}-{uuid.uuid4().hex[:8]}"
            public_url = await self._provision_container(proposal, deployment_id, worker_token)
            if not public_url:
                raise ToolError(
                    f"Container deployment failed to report a ready public URL within timeout for '{deployment_id}'."
                )

            # 3. Accept task on-chain now that container provisioning has succeeded
            self._accept_task(task_id, proposal, task_data.get("state"))

            # 4. Settle deliverable on TaskEscrow
            deliverable_record = {
                "deployment_id": deployment_id,
                "image_url": PILOT_IMAGE,
                "traffic_port": 8080,
                "hardware_id": proposal["hardware_id"],
                "duration_hours": proposal.get("duration_hours", 1),
                "deployed_at": now,
            }
            deliverable_hash = "0x" + hashlib.sha256(canonical(deliverable_record)).hexdigest()
            evidence_hash = "0x" + hashlib.sha256(b"warrant-transcription-deployment-evidence").hexdigest()

            journal_bytes = encode_journal(
                policy_hash=proposal["policy_hash"],
                chain_id=proposal["chain_id"],
                vault=proposal["escrow_address"],
                token=proposal["token_address"],
                recipient=proposal["recipient"],
                amount=proposal["amount"],
                task_id=task_id,
                deliverable_hash=deliverable_hash,
                policy_version=1,
                valid_after=now - 300,
                valid_until=now + 86400,
                evidence_hash=evidence_hash,
            )

            settlement_tx = None
            try:
                settle_res = self.escrow_client.settle_mock(
                    journal_bytes,
                    sender=proposal.get("recipient") or self.escrow_client.server_account,
                )
                settlement_tx = settle_res.get("transactionHash")
            except Exception as err:
                raise ToolError(f"Escrow settlement failed on-chain: {err}") from err

            # 4. Save active deployment session (1 hour duration)
            session = {
                "status": "deployed",
                "task_id": task_id,
                "deployment_id": deployment_id,
                "public_url": public_url,
                "worker_token": worker_token,
                "hardware_id": proposal["hardware_id"],
                "deployed_at": now,
                "expires_at": now + 3600,
                "duration_hours": 1,
                "settlement_tx": settlement_tx,
            }
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)

            return {
                "status": "deployed",
                "task_id": task_id,
                "deployment_id": deployment_id,
                "public_url": public_url,
                "expires_at": session["expires_at"],
                "duration_hours": 1,
                "settlement_transaction": settlement_tx,
                "message": (
                    "Deployment active and escrow settled.\n"
                    "Container will automatically terminate after 1 hour duration.\n"
                    "You can now call transcribe_audio(audio_path) while the server is active."
                ),
            }
        except Exception as exc:
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

    async def _poll_transcription(self, client: httpx2.AsyncClient, poll_url: str, headers: dict, job_id: str) -> dict:
        for _ in range(120):  # poll up to 60 seconds
            try:
                resp = await client.get(poll_url, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    job_status = resp.json()
                    st = job_status.get("status")
                    if st == "complete":
                        return self._transcription_result(job_id, job_status)
                    elif st in ("failed", "interrupted"):
                        raise ToolError(f"Transcription job failed: {job_status.get('error', st)}")
            except httpx2.HTTPError:
                pass
            await asyncio.sleep(0.5)

        raise ToolError("Transcription timed out waiting for worker.")

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

    def _active_session(self, task_id: str | None = None) -> dict:
        if task_id:
            session_file = self.state_dir / "sessions" / f"{task_id}.json"
            if not session_file.exists():
                raise ToolError(f"No deployment session found for task '{task_id}'.")
            session = read_json(session_file)
        else:
            session_file = self.state_dir / "active_session.json"
            session = None
            if session_file.exists():
                candidate = read_json(session_file)
                if time.time() <= candidate.get("expires_at", 0):
                    session = candidate
            if session is None:
                session = self._find_latest_unexpired_session()

            if session is None:
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
        audio_path: str,
        language: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        try:
            session = self._active_session(task_id)
            target_task_id = session.get("task_id", task_id)
            public_url = session["public_url"]
            worker_token = session.get("worker_token")

            path = pathlib.Path(audio_path)
            if not path.is_file():
                raise ToolError(f"Audio file not found: {audio_path}")

            audio_bytes = path.read_bytes()
            if len(audio_bytes) == 0:
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

            req_parsed = urllib.parse.urlsplit(worker_base)
            trust_env = req_parsed.hostname not in ("127.0.0.1", "localhost", "::1")

            try:
                async with httpx2.AsyncClient(timeout=30.0, trust_env=trust_env) as client:
                    resp = await client.post(req_url, content=audio_bytes, headers=headers)
                    if resp.is_error:
                        raise ToolError(f"Worker rejected upload with HTTP {resp.status_code}: {resp.text}")
                    resp_data = resp.json()
                    job_id = resp_data.get("job_id")
                    if not job_id:
                        raise ToolError(f"Unexpected worker response: {resp_data}")

                    poll_url = f"{worker_base}/job/{job_id}"
                    poll_headers = {"Authorization": f"Bearer {worker_token}"}
                    return await self._poll_transcription(client, poll_url, poll_headers, job_id)
            except (httpx2.HTTPError, OSError) as net_err:
                # Direct in-process worker fallback for sandboxed/mock testbeds
                local_worker = (
                    self.local_mock_workers.get(target_task_id) if target_task_id else None
                ) or self.active_local_worker
                if local_worker and getattr(local_worker, "worker", None):
                    return await self._transcribe_with_local_worker(
                        audio_bytes, audio_sha256, language, net_err, local_worker=local_worker
                    )
                raise ToolError(f"Worker connection failed: {net_err}") from net_err
        except Exception as exc:
            raise safe_tool_error(exc) from None

    async def _transcribe_with_local_worker(
        self,
        audio_bytes: bytes,
        audio_sha256: str,
        language: str | None,
        net_err: Exception,
        local_worker: LocalMockWorker | None = None,
    ) -> dict:
        lw = local_worker or self.active_local_worker
        if not lw or not getattr(lw, "worker", None):
            raise ToolError(f"Worker connection failed: {net_err}") from net_err
        worker = lw.worker
        code, job_data = worker.submit(audio_bytes, audio_sha256, language=language)
        if code not in (200, 202):
            raise ToolError(f"Local worker rejected job: {job_data}") from net_err
        job_id = job_data["job_id"]
        for _ in range(120):
            st_job = worker.get(job_id)
            if st_job and st_job.get("status") == "complete":
                return self._transcription_result(job_id, st_job)
            if st_job and st_job.get("status") in ("failed", "interrupted"):
                raise ToolError(f"Local worker job failed: {st_job.get('error')}") from net_err
            await asyncio.sleep(0.5)
        raise ToolError("Local worker job timed out.") from net_err

    async def get_deployment_status(self, task_id: str | None = None) -> dict:
        if task_id:
            session_file = self.state_dir / "sessions" / f"{task_id}.json"
            if not session_file.exists():
                return {"active": False, "message": f"No deployment session found for task '{task_id}'."}
            session = read_json(session_file)
        else:
            session_file = self.state_dir / "active_session.json"
            session = read_json(session_file) if session_file.exists() else self._find_latest_unexpired_session()
            if not session:
                return {"active": False, "message": "No deployment session active."}

        now = time.time()
        remaining = max(0, int(session.get("expires_at", 0) - now))
        is_expired = remaining == 0
        return {
            "active": not is_expired,
            "task_id": session.get("task_id"),
            "deployment_id": session.get("deployment_id"),
            "public_url": session.get("public_url"),
            "hardware_id": session.get("hardware_id"),
            "remaining_seconds": remaining,
            "is_expired": is_expired,
            "settlement_transaction": session.get("settlement_tx"),
        }


def create_server(
    state_dir: pathlib.Path,
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    verifier_address: str | None = None,
    server_account: str | None = None,
    mock_ionet: bool = False,
    local_server: bool = False,
    ssh_host: str = "petertower",
    worker_url: str | None = None,
) -> MCPServer:
    """Create and configure the Warrant Transcription MCP server."""
    server = MCPServer("seller-mcp-server")
    has_ionet_key = bool(os.environ.get("IO_NET_API_KEY"))
    use_mock_ionet = mock_ionet or not has_ionet_key

    escrow_client = EscrowClient(
        rpc_url=rpc_url,
        escrow_address=escrow_address,
        token_address=token_address,
        verifier_address=verifier_address,
        server_account=server_account,
    )

    service = WarrantTranscriptionService(
        state_dir=state_dir,
        escrow_client=escrow_client,
        use_mock_ionet=use_mock_ionet,
        local_server=local_server,
        ssh_host=ssh_host,
        worker_url=worker_url,
    )

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
            "and returns concrete TaskEscrow payment terms (taskId, salt, policyHash, amount, recipient)."
        ),
    )(service.propose_deployment)

    server.tool(
        name="deploy_with_escrow",
        description=(
            "Verify on-chain escrow funding for the proposed task, accept it, provision the io.net container "
            "for 1 hour duration, settle the escrow deliverable, and activate the transcription service.\n"
            "Prerequisite: propose_deployment must have been called and TaskEscrow.offer executed on-chain."
        ),
    )(service.deploy_with_escrow)

    server.tool(
        name="transcribe_audio",
        description=(
            "Transcribe an audio file using an active 1-hour Whisper deployment on io.net.\n"
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

    return server


def create_app(
    state_dir: pathlib.Path,
    auth_user: str | None = None,
    auth_pass: str | None = None,
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    verifier_address: str | None = None,
    server_account: str | None = None,
    mock_ionet: bool = False,
    local_server: bool = False,
    ssh_host: str = "petertower",
    worker_url: str | None = None,
):
    """Build ASGI app with streamable HTTP transport and basic auth."""
    server = create_server(
        state_dir=state_dir,
        rpc_url=rpc_url,
        escrow_address=escrow_address,
        token_address=token_address,
        verifier_address=verifier_address,
        server_account=server_account,
        mock_ionet=mock_ionet,
        local_server=local_server,
        ssh_host=ssh_host,
        worker_url=worker_url,
    )
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(transport_security=sec)

    if auth_user and auth_pass:
        print(f"HTTP Basic Auth enabled for user '{auth_user}'", file=sys.stderr)
        app = BasicAuthMiddleware(app, auth_user, auth_pass)
    return app


def run_server(
    host: str = "localhost",
    port: int = 8000,
    state_dir: pathlib.Path = pathlib.Path("artifacts/state"),
    auth_user: str | None = None,
    auth_pass: str | None = None,
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    verifier_address: str | None = None,
    server_account: str | None = None,
    mock_ionet: bool = False,
    local_server: bool = False,
    ssh_host: str = "petertower",
    worker_url: str | None = None,
):
    """Run MCP server with uvicorn."""
    app = create_app(
        state_dir=state_dir,
        auth_user=auth_user,
        auth_pass=auth_pass,
        rpc_url=rpc_url,
        escrow_address=escrow_address,
        token_address=token_address,
        verifier_address=verifier_address,
        server_account=server_account,
        mock_ionet=mock_ionet,
        local_server=local_server,
        ssh_host=ssh_host,
        worker_url=worker_url,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


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
        default=os.environ.get("WARRANT_RPC_URL", "http://127.0.0.1:8545"),
        help="Ethereum / Arc JSON-RPC URL (default: http://127.0.0.1:8545)",
    )
    parser.add_argument(
        "--escrow-address",
        default=os.environ.get("WARRANT_ESCROW"),
        help="TaskEscrow contract address",
    )
    parser.add_argument(
        "--token-address",
        default=os.environ.get("WARRANT_TOKEN"),
        help="Payment token contract address (e.g. USDC)",
    )
    parser.add_argument(
        "--verifier-address",
        default=os.environ.get("WARRANT_VERIFIER"),
        help="zkVM / Journal verifier contract address",
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
        "--ssh-deploy",
        dest="local_server",
        action="store_true",
        default=os.environ.get("LOCAL_SERVER", os.environ.get("SSH_DEPLOY", "")).lower() in ("1", "true"),
        help="Deploy container to local/remote server via SSH instead of live io.net CaaS",
    )
    parser.add_argument(
        "--ssh-host",
        default=os.environ.get("SSH_HOST", "petertower"),
        help="SSH host target for container deployment (default: petertower)",
    )
    parser.add_argument(
        "--worker-url",
        default=os.environ.get("WORKER_URL"),
        help="HTTP URL for the deployed worker (default: http://<ssh-host>:8080)",
    )
    args = parser.parse_args()

    run_server(
        host=args.host,
        port=args.port,
        state_dir=args.state_dir,
        auth_user=args.auth_user,
        auth_pass=args.auth_pass,
        rpc_url=args.rpc_url,
        escrow_address=args.escrow_address,
        token_address=args.token_address,
        verifier_address=args.verifier_address,
        server_account=args.server_account,
        mock_ionet=args.mock_ionet,
        local_server=args.local_server,
        ssh_host=args.ssh_host,
        worker_url=args.worker_url,
    )


if __name__ == "__main__":
    main()
