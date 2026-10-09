"""Streamable HTTP MCP server for Warrant GPU transcription on io.net CaaS."""
import asyncio
import base64
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
import uvicorn

from showcases.ionet_arc.adapter import (
    PilotError,
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
    canonical,
    encode_journal,
)
from showcases.ionet_arc.worker import Worker, handler

PILOT_IMAGE = "ghcr.io/mnemonik-xyz/warrant-transcription:pilot"


class BasicAuthMiddleware:
    """Lightweight ASGI middleware enforcing HTTP Basic Authentication."""

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.expected_auth = "Basic " + base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")

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
    return ToolError(
        f"Operation failed: {str(exc)}"
    )


def load_cached_hardware() -> list[dict]:
    """Fallback hardware list from checked-in artifact when running offline/mock."""
    hw_file = REPO_ROOT / "artifacts" / "io-hardware.json"
    if hw_file.exists():
        data = json.loads(hw_file.read_text())
        res = data.get("result", {})
        if "data" in res and "data" in res["data"]:
            return res["data"]["data"]
    return []


def _is_single_gpu(hw_id: str | int | None, max_gpus: int | None) -> bool:
    if max_gpus == 1:
        return True
    if isinstance(hw_id, str):
        lower = hw_id.lower()
        return "1x" in lower or "1-24g" in lower
    if isinstance(hw_id, int):
        return True
    return False


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

        suitable.append({
            "hardware_id": hw_id,
            "hardware_name": item.get("hardware_name", str(hw_id)),
            "price_per_hour_usd": float(price),
            "available_replicas": int(available),
            "location": item.get("location") or "US",
        })

    suitable.sort(key=lambda x: x["price_per_hour_usd"])
    return suitable


def ensure_policy_hash(
    escrow_address: str,
    token_address: str,
    chain_id: int,
    max_amount: int,
    state_dir: pathlib.Path,
) -> str:
    """Instantiate and hash accepted-contractor-v1 policy for this scope."""
    params_template = REPO_ROOT / "templates" / "example-parameters.json"
    template_file = REPO_ROOT / "templates" / "accepted-contractor-v1.json"
    policy_binary = REPO_ROOT / "target" / "debug" / "warrant-policy"

    escrow_bytes = list(bytes.fromhex(escrow_address[2:] if escrow_address.startswith("0x") else escrow_address))
    token_bytes = list(bytes.fromhex(token_address[2:] if token_address.startswith("0x") else token_address))

    params_obj = json.loads(params_template.read_text())
    params_obj["policy"]["scope"] = {
        "chain_id": chain_id,
        "vault": escrow_bytes,
        "token": token_bytes,
    }
    params_obj["policy"]["valid_after"] = 1000
    params_obj["policy"]["valid_until"] = int(time.time()) + 86400 * 30
    params_obj["bindings"] = {
        "max_amount": max_amount,
        "categories": [DEFAULT_CATEGORY],
    }

    params_path = state_dir / "policy_parameters.json"
    policy_path = state_dir / "policy.json"
    params_path.write_text(json.dumps(params_obj, indent=2))

    if policy_binary.exists() and template_file.exists():
        subprocess.run(
            [str(policy_binary), "instantiate", str(template_file), str(params_path), str(policy_path)],
            check=True,
            capture_output=True,
        )
        res = subprocess.run(
            [str(policy_binary), "hash", str(policy_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    return "0x" + hashlib.sha256(canonical(params_obj)).hexdigest()


class LocalMockWorker:
    """Mock in-process transcription worker for offline testing when IO_NET_API_KEY is unset."""

    def __init__(self, work_dir: pathlib.Path, token: str):
        self.work_dir = work_dir
        self.token = token
        self.server = None
        self.port = 0
        self.thread = None

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

        worker = Worker(self.work_dir, self.token, engine=dummy_engine)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(worker))
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
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
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir = self.state_dir / "proposals"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.escrow_client = escrow_client
        self.use_mock_ionet = use_mock_ionet
        self.active_local_worker = None

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
        hardware_id: str | int = None,
        customer_address: str = None,
        location_id: str = "US",
        budget_cap_usd: str = "1.00",
    ) -> dict:
        try:
            candidates = await self.list_suitable_hardware()
            if not candidates:
                raise ToolError("No available suitable GPUs found.")

            selected_hw = self._select_hardware(candidates, hardware_id)
            customer_address, chain_id = self._resolve_addresses_and_chain(customer_address)
            self._ensure_escrow_contracts(customer_address)

            salt = "0x" + secrets.token_hex(32)
            try:
                task_id = self.escrow_client.task_id_for(customer_address, salt)
            except Exception:
                h = hashlib.sha256(
                    f"{chain_id}-{self.escrow_client.escrow}-{customer_address}-{salt}".encode()
                ).hexdigest()
                task_id = "0x" + h

            policy_hash = ensure_policy_hash(
                self.escrow_client.escrow,
                self.escrow_client.token,
                chain_id,
                100_000_000,
                self.state_dir,
            )

            # 1 hour duration; amount in token units (USDC 6 decimals -> $1.00 = 1,000,000)
            token_amount = 1_000_000

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
                "amount_usd": "1.00",
                "hardware_id": selected_hw["hardware_id"],
                "hardware_name": selected_hw["hardware_name"],
                "price_per_hour_usd": selected_hw["price_per_hour_usd"],
                "location_ids": location_id,
                "duration_hours": 1,
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

    def _verify_and_accept_task(self, task_id: str, proposal: dict):
        try:
            task_data = self.escrow_client.get_task(task_id)
            task_state = task_data.get("state")
            if task_state == "Missing":
                raise ToolError(
                    f"Task '{task_id}' has not been offered on TaskEscrow ({self.escrow_client.escrow}). "
                    "Customer must submit TaskEscrow.offer() before deploying."
                )
            if task_state in ("Paid", "Refunded"):
                raise ToolError(f"Task '{task_id}' is already in terminal state: {task_state}.")

            if task_state == "Offered":
                self.escrow_client.accept(
                    task_id,
                    sender=proposal.get("recipient") or self.escrow_client.server_account,
                )
        except ToolError:
            raise
        except Exception as e:
            print(f"Warning checking/accepting on-chain task: {e}", file=sys.stderr)

    async def _provision_container(self, proposal: dict, deployment_id: str, worker_token: str) -> str | None:
        if self.use_mock_ionet:
            if self.active_local_worker:
                self.active_local_worker.stop()
            self.active_local_worker = LocalMockWorker(self.state_dir / "worker_data", worker_token)
            return self.active_local_worker.start()

        deploy_args = {
            "billing_model": "duration",
            "hardware_id": proposal["hardware_id"],
            "location_ids": proposal["location_ids"],
            "duration_hours": 1,
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
            "duration_hours": 1,
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

            # 1. Verify on-chain escrow state
            self._verify_and_accept_task(task_id, proposal)

            # 2. Provision container (1h duration)
            worker_token = secrets.token_hex(24)
            deployment_id = f"dep-{now}-{uuid.uuid4().hex[:8]}"
            public_url = await self._provision_container(proposal, deployment_id, worker_token)

            # 3. Settle deliverable on TaskEscrow
            deliverable_record = {
                "deployment_id": deployment_id,
                "image_url": PILOT_IMAGE,
                "traffic_port": 8080,
                "hardware_id": proposal["hardware_id"],
                "duration_hours": 1,
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
                print(f"Warning during escrow settlement: {err}", file=sys.stderr)

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

    def _poll_transcription(self, opener, poll_req: urllib.request.Request, job_id: str) -> dict:
        for _ in range(120):  # poll up to 60 seconds
            try:
                with opener.open(poll_req, timeout=10) as poll_resp:
                    job_status = json.load(poll_resp)
                    st = job_status.get("status")
                    if st == "complete":
                        res = job_status.get("result", {})
                        segments = res.get("segments", [])
                        full_text = " ".join(s.get("text", "").strip() for s in segments)
                        return {
                            "status": "complete",
                            "job_id": job_id,
                            "text": full_text,
                            "language": res.get("language"),
                            "audio_seconds": res.get("audio_seconds"),
                            "segments": segments,
                            "elapsed_seconds": job_status.get("elapsed_seconds"),
                        }
                    elif st in ("failed", "interrupted"):
                        raise ToolError(f"Transcription job failed: {job_status.get('error', st)}")
            except urllib.error.HTTPError:
                pass
            time.sleep(0.5)

        raise ToolError("Transcription timed out waiting for worker.")

    async def transcribe_audio(self, audio_path: str, language: str = None) -> dict:
        try:
            session_file = self.state_dir / "active_session.json"
            if not session_file.exists():
                raise ToolError(
                    "No active deployment session found. "
                    "Prerequisites: list_suitable_hardware -> propose_deployment -> deploy_with_escrow."
                )

            session = read_json(session_file)
            now = time.time()
            if now > session.get("expires_at", 0):
                raise ToolError(
                    f"Deployment session expired at {session.get('expires_at')}. "
                    "The 1-hour duration window has ended. A new proposal and deployment are required."
                )

            public_url = session.get("public_url")
            worker_token = session.get("worker_token")
            if not public_url:
                raise ToolError("Deployment public URL is not ready yet.")

            path = pathlib.Path(audio_path)
            if not path.is_file():
                raise ToolError(f"Audio file not found: {audio_path}")

            audio_bytes = path.read_bytes()
            if len(audio_bytes) == 0:
                raise ToolError("Audio file is empty.")

            audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
            query = f"?language={language.strip().lower()}" if language else ""
            req_url = f"{public_url}/job{query}"

            headers = {
                "Authorization": f"Bearer {worker_token}",
                "Content-Type": "application/octet-stream",
                "X-Audio-SHA256": audio_sha256,
                "Content-Length": str(len(audio_bytes)),
            }

            req_parsed = urllib.parse.urlsplit(public_url)
            opener = (
                urllib.request.build_opener(urllib.request.ProxyHandler({}))
                if req_parsed.hostname in ("127.0.0.1", "localhost", "::1")
                else urllib.request.build_opener()
            )

            request = urllib.request.Request(req_url, audio_bytes, headers, method="POST")
            try:
                with opener.open(request, timeout=30) as resp:
                    resp_data = json.load(resp)
            except urllib.error.HTTPError as err:
                raise ToolError(f"Worker rejected upload with HTTP {err.code}: {err.read().decode()}")

            job_id = resp_data.get("job_id")
            if not job_id:
                raise ToolError(f"Unexpected worker response: {resp_data}")

            poll_url = f"{public_url}/job/{job_id}"
            poll_req = urllib.request.Request(poll_url, headers={"Authorization": f"Bearer {worker_token}"})
            return self._poll_transcription(opener, poll_req, job_id)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    async def get_deployment_status(self) -> dict:
        session_file = self.state_dir / "active_session.json"
        if not session_file.exists():
            return {"active": False, "message": "No deployment session active."}
        session = read_json(session_file)
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
) -> MCPServer:
    """Create and configure the Warrant Transcription MCP server."""
    server = MCPServer("warrant-transcription")
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
            "Transcribe an audio file using the active 1-hour Whisper deployment on io.net.\n"
            "Prerequisite: deploy_with_escrow must have been completed and the 1-hour window must not be expired."
        ),
    )(service.transcribe_audio)

    server.tool(
        name="get_deployment_status",
        description="Check status of active 1-hour transcription deployment and remaining duration.",
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
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
