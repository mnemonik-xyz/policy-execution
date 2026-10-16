"""Streamable HTTP MCP server for Warrant GPU container deployment on io.net CaaS."""

import argparse
import asyncio
import atexit
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

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
    EscrowClient,
    address20,
    bytes32,
    evaluate_authorization,
    get_manifest_parameter,
    hash_policy,
    load_manifest,
    should_trust_env,
)

PILOT_IMAGE = "ghcr.io/mnemonik-xyz/transcription:pilot"

# Mainnet policy (tech-spec Decision 6). Every branch keys on the chain that the RPC reports at construction,
# never on the manifest alone: a manifest-only guard is bypassable through WARRANT_RPC_URL.
MAINNET_CHAIN_ID = 5042
MAINNET_LABEL = "after seller acceptance anyone can trigger payout; no proof; amounts up to 1 USD"
MAINNET_LIMITS = (
    "Settlement uses a test-double verifier that accepts any digest the seller approves.",
    "The 1 USD cap and the one-hour lease are seller policy, not contract rules.",
    "One deal at a time.",
    "The mainnet seller path has not been exercised with money before this listing.",
    "Any holder of the basic-auth credential can read the current deployment status, including its worker token.",
)
MAINNET_INSTRUCTIONS = MAINNET_LABEL + ".\n" + " ".join(MAINNET_LIMITS)
TASK_ID_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")


class StartRefusedError(RuntimeError):
    """The server must not start: the configuration would run it unsafely."""


class MainnetPolicyError(StartRefusedError):
    """The server must not start: the configuration breaks the chain 5042 policy."""


class ChainDetectionError(MainnetPolicyError):
    """The server must not start: the RPC chain is unknown, so the chain policy cannot be chosen."""


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


def _is_single_gpu(hw_id: str | int | None, max_gpus: int | None) -> bool:
    if max_gpus == 1:
        return True
    if isinstance(hw_id, str):
        lower = hw_id.lower()
        return "1x" in lower or "1-24g" in lower
    return bool(isinstance(hw_id, int))


def _is_supported_gpu(hw_name: str, hw_id: str | int | None) -> bool:
    supported_names = ["3090", "4090", "L40", "A6000", "A100", "H100", "T4", "V100", "A40", "A30", "L4", "ADA"]
    if any(g in hw_name for g in supported_names):
        return True
    return isinstance(hw_id, int)


def filter_suitable_gpus(hardware_items: list[dict]) -> list[dict]:
    """Filter suitable single GPUs for container deployment, sorted by price."""
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
        if not _is_supported_gpu(hw_name, hw_id):
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


def _credit_amount(balances: dict, field: str) -> Decimal:
    value = balances.get(field)
    # io.net reports the balances as JSON numbers; a bool, a string or a missing field is unreadable.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"get_credit_status field {field} is missing or not a number")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"get_credit_status field {field} is not a finite amount of 0 or more")
    return amount


def parse_credit_balance(status) -> Decimal:
    """Return the spendable USD credits from an io.net get_credit_status result. Raise ValueError when unreadable.

    Shape (read 2026-10-15): {"service", "operation", "data": {"balances": {"credits_balance": float,
    "credits_courtesy_balance": float, ...}, "burn_rate": {...}}}. Courtesy credits do not pay for a deployment
    (the price estimate reports min_non_promotional_credits_required), so they do not count toward the floor.
    """
    data = status.get("data") if isinstance(status, dict) else None
    balances = data.get("balances") if isinstance(data, dict) else None
    if not isinstance(balances, dict):
        raise ValueError("get_credit_status result has no data.balances object")
    spendable = _credit_amount(balances, "credits_balance") - _credit_amount(balances, "credits_courtesy_balance")
    if spendable < 0:
        raise ValueError("get_credit_status courtesy credits exceed the credit balance")
    return spendable


class SellerWarrantService:
    """Encapsulates tool implementations and state for the Seller Warrant MCP server."""

    def __init__(
        self,
        state_dir: pathlib.Path,
        escrow_client: EscrowClient,
        local_server: bool = False,
        ssh_host: str = "localhost",
        worker_url: str | None = None,
        vendor_credential_path: pathlib.Path | None = None,
        vendor_signature_path: pathlib.Path | None = None,
        buyer_account: str | None = None,
        manifest_path: pathlib.Path | None = None,
        chain_id: int | None = None,
        max_budget_usd: Decimal | float | str | None = None,
        duration_hours: int | None = None,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.proposals_dir = self.state_dir / "proposals"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir = self.state_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.escrow_client = escrow_client
        self.local_server = local_server
        self.ssh_host = ssh_host
        self.worker_url = worker_url
        self.vendor_credential_path = pathlib.Path(vendor_credential_path) if vendor_credential_path else None
        self.vendor_signature_path = pathlib.Path(vendor_signature_path) if vendor_signature_path else None
        self.manifest_path = pathlib.Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
        # The chain read from the RPC at construction. The tools use it for the policy decision and do not
        # read the chain again, so an RPC failure later cannot switch the mainnet policy off.
        self.chain_id = chain_id
        self.mainnet = chain_id == MAINNET_CHAIN_ID
        # Held on chain 5042 from the single-session check to the save of the provisioning session.
        # It serialises one server process only: the deployment runs one seller container per state dir.
        self._deal_lock = asyncio.Lock()

        manifest = load_manifest(self.manifest_path)
        if max_budget_usd is not None:
            self.max_budget_usd = Decimal(str(max_budget_usd))
        else:
            raw_budget = get_manifest_parameter(manifest, "max_budget_usd", "max-budget-usd")
            if raw_budget is not None:
                self.max_budget_usd = Decimal(str(raw_budget))
            else:
                raw_amount = get_manifest_parameter(manifest, "max_amount", "max-amount")
                if raw_amount is not None:
                    self.max_budget_usd = Decimal(str(raw_amount)) / Decimal(1_000_000)
                elif self.mainnet:
                    self.max_budget_usd = Decimal("1.00")
                else:
                    self.max_budget_usd = Decimal("2.00")

        if duration_hours is not None:
            self.duration_hours = int(duration_hours)
        else:
            raw_duration = get_manifest_parameter(
                manifest, "duration_hours", "duration-hours", "max_duration_hours", "max-duration-hours"
            )
            self.duration_hours = int(raw_duration) if raw_duration is not None else 1

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

        self._bg_tasks: set[asyncio.Task] = set()
        self._ssh_container_started = False
        if self.local_server:
            atexit.register(self._cleanup_ssh_container)

    def _cleanup_ssh_container(self):
        if self.local_server and self._ssh_container_started:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["ssh", self.ssh_host, "docker", "stop", "warrant-worker"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

    async def list_suitable_hardware(self) -> list[dict]:
        logger.info("Tool called: list_suitable_hardware")
        try:
            logger.info("Calling io.net CaaS API to list available hardware")

            async def op(cloud):
                res = await cloud.call("caas_get_hardware_ids", {})
                data = res.get("data", {})
                return data.get("data", []) if isinstance(data, dict) else []

            raw_items = await connected(op)

            logger.info("Filtering %d raw hardware options", len(raw_items))
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
        if self.mainnet:
            chain_id = self.chain_id
        else:
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

    def _require_task_id(self, task_id: str | None) -> str | None:
        """On chain 5042, require a TaskEscrow task id and return it in lower case; elsewhere return it as is."""
        if not self.mainnet:
            return task_id
        if not task_id:
            raise ToolError(f"task_id is required on chain {MAINNET_CHAIN_ID}.")
        # task_id names state files, so the public service accepts only a TaskEscrow task id.
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ToolError(f"task_id must be a 0x-prefixed 32-byte hex string on chain {MAINNET_CHAIN_ID}.")
        return task_id.lower()

    def _mainnet_budget_cap(self, budget_cap_usd: str | float | None) -> Decimal:
        """The effective cap on chain 5042: the caller's cap if lower, never above manifest max_budget_usd."""
        if budget_cap_usd is None:
            return self.max_budget_usd
        try:
            cap = Decimal(str(budget_cap_usd))
        except (ValueError, InvalidOperation):
            raise ToolError(f"Invalid budget_cap_usd: {budget_cap_usd}") from None
        if not cap.is_finite():
            raise ToolError(f"Invalid budget_cap_usd: {budget_cap_usd}")
        return min(cap, self.max_budget_usd)

    async def _require_credits(self, required_usd: Decimal):
        """Refuse a proposal when io.net credits are below the required cost or cannot be read (fail closed)."""
        try:
            status = await connected(lambda cloud: cloud.call("get_credit_status", {}))
            balance = parse_credit_balance(status)
            if not isinstance(balance, Decimal) or not balance.is_finite() or balance < 0:
                raise ValueError("the credit balance is not a finite amount of 0 or more")
        except Exception as exc:
            # A ValueError from the parser names the field only, never an amount or an account identifier.
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            logger.warning("io.net credit status could not be read: %s", detail)
            raise ToolError(
                f"The io.net credit balance cannot be read, so no deal is proposed on chain {MAINNET_CHAIN_ID}."
            ) from None
        if balance < required_usd:
            logger.warning("io.net credits %s USD are below required %s USD", balance, required_usd)
            raise ToolError(
                f"The seller's io.net credits are below {required_usd:.2f} USD, so no deal is proposed. "
                "Try again later."
            )

    async def propose_deployment(
        self,
        hardware_id: str | int | None = None,
        customer_address: str | None = None,
        location_id: str = "US",
        duration_hours: int = 1,
        budget_cap_usd: str | float | None = None,
    ) -> dict:
        logger.info(
            "Tool called: propose_deployment with hardware_id=%.80r, customer_address=%.80r, location_id=%.80r, "
            "duration_hours=%.80r, budget_cap_usd=%.80r",
            hardware_id,
            customer_address,
            location_id,
            duration_hours,
            budget_cap_usd,
        )
        try:
            mainnet_cap = None
            if self.mainnet:
                # Checked before the hardware call and the credit call.
                if type(duration_hours) is not int or duration_hours != self.duration_hours:
                    hour_label = "hour" if self.duration_hours == 1 else "hours"
                    raise ToolError(
                        f"duration_hours must be {self.duration_hours} {hour_label} on chain {MAINNET_CHAIN_ID}, "
                        f"got {duration_hours}"
                    )
                mainnet_cap = self._mainnet_budget_cap(budget_cap_usd)

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
            # Dynamic pricing based on real hardware cost and duration
            price_per_hour = Decimal(str(selected_hw["price_per_hour_usd"]))
            duration = Decimal(str(duration_hours))
            cost_usd = price_per_hour * duration

            if mainnet_cap is not None:
                if not price_per_hour.is_finite() or price_per_hour <= 0:
                    raise ToolError(f"Selected hardware '{selected_hw['hardware_name']}' has no valid price.")
                if cost_usd > mainnet_cap:
                    logger.warning("Hardware cost $%.2f exceeds mainnet cap $%.2f", cost_usd, mainnet_cap)
                    raise ToolError(
                        f"Selected hardware '{selected_hw['hardware_name']}' cost ${cost_usd:.2f} exceeds the "
                        f"budget cap of ${mainnet_cap:.2f} (chain {MAINNET_CHAIN_ID} deals are capped at "
                        f"${self.max_budget_usd:.2f})."
                    )
                await self._require_credits(cost_usd)

            customer_address, chain_id = await self._resolve_addresses_and_chain(customer_address)
            self._ensure_escrow_contracts(chain_id)

            salt = "0x" + secrets.token_hex(32)
            task_id = await asyncio.to_thread(self.escrow_client.task_id_for, customer_address, salt)
            logger.info("Derived taskIdFor(%s, salt=%s): %s", customer_address, salt, task_id)

            if mainnet_cap is None and budget_cap_usd is not None:
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
                "image_url": PILOT_IMAGE,
                "traffic_port": 8080,
                "proposed_at": int(time.time()),
                "instructions": (
                    "Verify this proposal against your Warrant policy (max budget, category, duration).\n"
                    "Call TaskEscrow.offer(salt, recipient, policyHash, policyVersion, amount, "
                    "acceptBy, settleBy) on-chain.\n"
                    "Call deploy_with_escrow(task_id, policy) to commit agreement and begin provisioning.\n"
                    "Poll get_deployment_status until ready, verify container /health, and sign Acceptance evidence.\n"
                    "Submit acceptance via submit_acceptance to settle escrow on-chain, then operate the container "
                    "at <public_url> with Bearer <worker_token>."
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

    def _ionet_state_path(self, deployment_id: str) -> pathlib.Path:
        """The io.net deploy state file; adapter.deploy writes it before the remote deploy call."""
        return self.state_dir / f"{deployment_id}.json"

    async def _provision_container(self, proposal: dict, deployment_id: str, worker_token: str) -> str | None:
        if self.local_server:
            # 1. Clean up any existing container on SSH host
            await asyncio.to_thread(
                subprocess.run,
                ["ssh", self.ssh_host, "docker", "rm", "-f", "warrant-worker"],
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
                "warrant-worker",
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

        duration = proposal.get("duration_hours", 1)
        deploy_args = {
            "billing_model": "duration",
            "hardware_id": proposal["hardware_id"],
            "location_ids": proposal["location_ids"],
            "duration_hours": duration,
            "gpus_per_container": 1,
            "replica_count": 1,
            "resource_private_name": f"warrant-worker-{proposal['task_id'][-8:]}",
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
        state_file = self._ionet_state_path(deployment_id)

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
            if self.mainnet and self._ionet_state_path(deployment_id).exists():
                # deploy() saves its state file before the remote call, so a billable io.net deployment can
                # exist although provisioning failed. Keep the deal slot closed until that lease can have ended.
                session["deployment_may_exist"] = True
                session["expires_at"] = int(time.time()) + proposal.get("duration_hours", 1) * 3600
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)

    def _blocking_session(self) -> str | None:
        """Describe a session that blocks a new deal on chain 5042, or return None.

        Scans every file in sessions/: active_session.json holds the latest session only, and
        _lookup_session treats a session without expires_at as expired. Fails closed on any doubt.
        """
        now = time.time()
        for path in sorted(self.sessions_dir.glob("*.json")):
            try:
                session = read_json(path)
            except Exception:
                return f"session file {path.name} is unreadable"
            if not isinstance(session, dict):
                return f"session file {path.name} is unreadable"
            status = session.get("status")
            task = session.get("task_id") or path.stem
            expires_at = session.get("expires_at")
            expired = isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and expires_at <= now
            if status == "failed":
                if not session.get("deployment_may_exist") or expired:
                    continue
                return f"task {task} failed, but its io.net deployment can still run until its lease ends"
            if status in ("ready", "settled") and expired:
                continue
            # provisioning (with or without expires_at), a running lease, or an unknown state
            return f"task {task} is {status or 'in an unknown state'}"
        return None

    def _check_mainnet_terms(self, proposal: dict):
        """Refuse a stored proposal whose terms break the chain 5042 limits (for example one from another run)."""
        amount = proposal.get("amount")
        max_amount = int(self.max_budget_usd * 1_000_000)
        hour_label = "hour" if self.duration_hours == 1 else "hours"
        if (
            proposal.get("chain_id") != MAINNET_CHAIN_ID
            or type(proposal.get("duration_hours")) is not int
            or proposal.get("duration_hours") != self.duration_hours
            or type(amount) is not int
            or not 0 < amount <= max_amount
        ):
            raise ToolError(
                f"The stored proposal does not match the chain {MAINNET_CHAIN_ID} limits "
                f"({self.duration_hours} {hour_label}, at most ${self.max_budget_usd:.2f}); the offer is not accepted."
            )

    async def _commit_deal(self, task_id: str, policy: dict | None) -> tuple[dict, dict]:
        """Verify the offer, accept it on-chain and save the provisioning session."""
        prop_path = self.proposals_dir / f"{task_id}.json"
        if not prop_path.exists():
            logger.warning("No proposal record found for task_id '%s' at %s", task_id, prop_path)
            raise ToolError(f"No proposal record found for task_id '{task_id}'. Call propose_deployment first.")

        proposal = read_json(prop_path)
        if self.mainnet:
            self._check_mainnet_terms(proposal)
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
        return proposal, session

    async def deploy_with_escrow(self, task_id: str, policy: dict | None = None) -> dict:
        logger.info("Tool called: deploy_with_escrow with task_id=%.80r", task_id)
        try:
            task_id = self._require_task_id(task_id)
            if self.mainnet:
                # One deal at a time. The lock makes the check and the save of the provisioning session atomic
                # for concurrent calls; a failure before the save releases the slot. The check comes before
                # the on-chain accept, so that the buyer can still refund an unaccepted offer.
                if self._deal_lock.locked():
                    # Another deal is between its check and its session save (the on-chain accept can take
                    # about 30 s); refuse at once instead of making the caller wait for that.
                    raise ToolError(
                        f"One deal at a time on chain {MAINNET_CHAIN_ID}: another deal is being accepted. "
                        "The offer is not accepted; try again after the active deal ends."
                    )
                async with self._deal_lock:
                    blocking = self._blocking_session()
                    if blocking:
                        raise ToolError(
                            f"One deal at a time on chain {MAINNET_CHAIN_ID}: {blocking}. "
                            "The offer is not accepted; try again after the active deal ends."
                        )
                    proposal, session = await self._commit_deal(task_id, policy)
            else:
                proposal, session = await self._commit_deal(task_id, policy)
            deployment_id = session["deployment_id"]
            worker_token = session["worker_token"]
            deliverable_hash = session["deliverable_hash"]

            # 5. Spawn background provisioning
            logger.info("Spawning background container provisioning for deployment_id=%s", deployment_id)
            bg_task = asyncio.create_task(self._provision_container_bg(task_id, proposal, deployment_id, worker_token))
            self._bg_tasks.add(bg_task)
            bg_task.add_done_callback(self._bg_tasks.discard)

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

    async def get_deployment_status(self, task_id: str | None = None) -> dict:
        logger.info("Tool called: get_deployment_status with task_id=%.80r", task_id)
        # On chain 5042 the caller must name the task: the status includes the worker token.
        task_id = self._require_task_id(task_id)
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

    async def submit_acceptance(
        self,
        task_id: str,
        acceptance: dict,
        signature: list[int] | str,
    ) -> dict:
        """Verify buyer acceptance evidence off-chain, approve journal, and settle TaskEscrow on-chain."""
        sig_len = len(signature) if isinstance(signature, (list, bytes)) else len(str(signature))
        logger.info("Tool called: submit_acceptance with task_id=%.80r, signature_length=%d", task_id, sig_len)
        try:
            task_id = self._require_task_id(task_id)
            session = self._lookup_session(task_id)
            if not session:
                logger.warning("No deployment session found for task '%s'", task_id)
                raise ToolError(f"No deployment session found for task '{task_id}'.")

            if session.get("status") == "settled" and session.get("settlement_tx"):
                res = {
                    "status": "settled",
                    "task_id": task_id,
                    "settlement_transaction": session.get("settlement_tx"),
                    "message": "Task is already settled.",
                }
                logger.info("submit_acceptance returning: task already settled (tx=%s)", session.get("settlement_tx"))
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

            # Settle on-chain via TaskEscrow (Option B: JournalVerifier approves digest, settles with mock seal)
            settlement_tx = None
            try:
                logger.info("Settling TaskEscrow on-chain via settle_mock with journal digest")
                settle_res = await asyncio.to_thread(
                    self.escrow_client.settle_mock,
                    journal_bytes,
                    sender=session.get("recipient") or self.escrow_client.server_account,
                )
                settlement_tx = settle_res.get("transactionHash")
                logger.info("TaskEscrow settled on-chain in tx %s", settlement_tx)
            except Exception as err:
                logger.error("Escrow settlement failed on-chain: %s", err)
                raise ToolError(f"Escrow settlement failed on-chain: {err}") from err

            session["status"] = "settled"
            session["settlement_tx"] = settlement_tx
            session["journal_hex"] = "0x" + journal_bytes.hex()
            save(self.state_dir / "active_session.json", session)
            save(self.state_dir / "sessions" / f"{task_id}.json", session)

            res = {
                "status": "settled",
                "task_id": task_id,
                "settlement_transaction": settlement_tx,
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
    local_server: bool = False
    ssh_host: str = "localhost"
    worker_url: str | None = None
    vendor_credential_path: pathlib.Path | None = None
    vendor_signature_path: pathlib.Path | None = None
    manifest_path: pathlib.Path | None = None
    escrow_client: EscrowClient | None = None
    max_budget_usd: Decimal | float | str | None = None
    duration_hours: int | None = None

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
        if self.max_budget_usd is None:
            raw_budget = get_manifest_parameter(manifest, "max_budget_usd", "max-budget-usd")
            if raw_budget is not None:
                self.max_budget_usd = Decimal(str(raw_budget))
            else:
                raw_amount = get_manifest_parameter(manifest, "max_amount", "max-amount")
                if raw_amount is not None:
                    self.max_budget_usd = Decimal(str(raw_amount)) / Decimal(1_000_000)
        if self.duration_hours is None:
            raw_duration = get_manifest_parameter(
                manifest, "duration_hours", "duration-hours", "max_duration_hours", "max-duration-hours"
            )
            if raw_duration is not None:
                self.duration_hours = int(raw_duration)

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
            if self.seller_key_path and self.seller_key_path.exists() and self.seller_key_path.is_file():
                with contextlib.suppress(Exception):
                    self.seller_private_key = self.seller_key_path.read_text().strip()
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


def detect_rpc_chain(escrow_client: EscrowClient) -> int:
    """Read the chain from the RPC once at construction. An unreadable chain refuses the start."""
    try:
        return int(escrow_client.rpc.chain_id())
    except Exception as exc:
        # The RPC URL can carry an API key, so the message names the error type only.
        raise ChainDetectionError(
            f"cannot determine the chain: the configured RPC did not answer eth_chainId ({type(exc).__name__})"
        ) from None


def enforce_mainnet_start_policy(cfg: ServerConfig, chain_id: int, has_ionet_key: bool):
    """Refuse a start on chain 5042 that would run a stub or a mismatched manifest (Decision 6)."""
    if cfg.local_server:
        raise MainnetPolicyError(f"local mode is refused on chain {chain_id}")
    if not has_ionet_key:
        raise MainnetPolicyError(f"IO_NET_API_KEY is required on chain {chain_id}")
    # Same call and default as ServerConfig and the service. load_manifest returns {} for a missing or
    # unparseable file, so a manifest without chain_id is refused (fail closed).
    manifest = load_manifest(cfg.manifest_path)
    raw_chain = manifest.get("chain_id")
    try:
        manifest_chain = int(raw_chain)
    except (TypeError, ValueError):
        manifest_chain = None
    if manifest_chain != chain_id:
        raise MainnetPolicyError(
            f"manifest chain_id {raw_chain!r} does not match the RPC chain {chain_id} "
            f"(manifest: {cfg.manifest_path or DEFAULT_MANIFEST_PATH})"
        )


def missing_auth_variables(cfg: ServerConfig) -> list[str]:
    """Names of the basic-auth settings that are unset, empty or whitespace only."""
    values = {"MCP_AUTH_USER": cfg.auth_user, "MCP_AUTH_PASS": cfg.auth_pass}
    return [name for name, value in values.items() if not (value and value.strip())]


def is_loopback_host(host: str | None) -> bool:
    """True only for localhost and loopback IP literals; any other name fails closed."""
    name = (host or "").strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False


def enforce_http_auth_policy(cfg: ServerConfig, chain_id: int):
    """Refuse to serve /mcp without basic auth on chain 5042, or on a non-loopback address on any chain."""
    missing = missing_auth_variables(cfg)
    if not missing:
        return
    names = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    if chain_id == MAINNET_CHAIN_ID:
        raise MainnetPolicyError(f"{names} {verb} required on chain {chain_id}")
    if not is_loopback_host(cfg.host):
        raise StartRefusedError(f"{names} {verb} required when binding the non-loopback host {cfg.host!r}")
    if len(missing) == 1:
        logger.warning("seller-mcp-server: warning: %s is not set, so basic auth is off", names)


def create_server(
    config: ServerConfig | pathlib.Path | str | None = None,
    *,
    serve_http: bool = False,
    **kwargs,
) -> MCPServer:
    """Create and configure the Seller Warrant MCP server.

    serve_http=True (create_app) also applies the HTTP basic-auth start policy once the chain is known.
    """
    cfg = _normalize_config(config, **kwargs)
    has_ionet_key = bool(os.environ.get("IO_NET_API_KEY"))

    escrow_client = cfg.escrow_client or EscrowClient(
        rpc_url=cfg.rpc_url,
        escrow_address=cfg.escrow_address,
        token_address=cfg.token_address,
        verifier_address=cfg.verifier_address,
        server_account=cfg.server_account,
    )
    chain_id = detect_rpc_chain(escrow_client)
    mainnet = chain_id == MAINNET_CHAIN_ID
    if mainnet:
        enforce_mainnet_start_policy(cfg, chain_id, has_ionet_key)
    if serve_http:
        enforce_http_auth_policy(cfg, chain_id)
    server = MCPServer("seller-mcp-server", instructions=MAINNET_INSTRUCTIONS if mainnet else None)
    if cfg.seller_private_key:
        if cfg.server_account:
            escrow_client.register_key(cfg.server_account, cfg.seller_private_key)
        with contextlib.suppress(Exception):
            from eth_account import Account

            derived_addr = Account.from_key(cfg.seller_private_key).address
            escrow_client.register_key(derived_addr, cfg.seller_private_key)

    service = SellerWarrantService(
        state_dir=cfg.state_dir,
        escrow_client=escrow_client,
        local_server=cfg.local_server,
        ssh_host=cfg.ssh_host,
        worker_url=cfg.worker_url,
        vendor_credential_path=cfg.vendor_credential_path,
        vendor_signature_path=cfg.vendor_signature_path,
        buyer_account=cfg.buyer_account,
        manifest_path=cfg.manifest_path,
        chain_id=chain_id,
        max_budget_usd=cfg.max_budget_usd,
        duration_hours=cfg.duration_hours,
    )

    def describe(text: str, deal_tool: bool = False) -> str:
        # On chain 5042 the three deal tools start with the honest label; other chains keep their text.
        return f"{MAINNET_LABEL}.\n{text}" if mainnet and deal_tool else text

    server.tool(
        name="list_suitable_hardware",
        description=(
            "List available io.net single-GPU hardware options for container deployment.\n"
            "Filters for single-GPU configurations with live availability and sorts by price ascending."
        ),
    )(service.list_suitable_hardware)

    server.tool(
        name="propose_deployment",
        description=describe(
            "Generate an escrow proposal to deploy a container for 1 hour duration.\n"
            "Selects the cheapest suitable available GPU (or a specific hardware_id), calculates pricing, "
            "and returns concrete TaskEscrow payment terms (taskId, salt, amount, recipient).",
            deal_tool=True,
        ),
    )(service.propose_deployment)

    server.tool(
        name="deploy_with_escrow",
        description=describe(
            "Verify on-chain escrow funding for the proposed task, accept it on-chain, and provision "
            "the io.net container in the background for 1 hour duration.\n"
            "Prerequisite: propose_deployment must have been called and TaskEscrow.offer executed on-chain.",
            deal_tool=True,
        ),
    )(service.deploy_with_escrow)

    server.tool(
        name="get_deployment_status",
        description=(
            "Check status of active 1-hour container deployment and remaining duration.\n"
            "Optionally provide task_id to inspect a specific deployment session."
        ),
    )(service.get_deployment_status)

    server.tool(
        name="submit_acceptance",
        description=describe(
            "Submit buyer-signed Acceptance evidence for a verified deliverable.\n"
            "Evaluates policy and signatures off-chain via Warrant, approves the 384-byte journal, "
            "and settles payment on TaskEscrow on-chain.",
            deal_tool=True,
        ),
    )(service.submit_acceptance)

    return server


def create_app(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Build ASGI app with streamable HTTP transport and basic auth."""
    cfg = _normalize_config(config, **kwargs)
    server = create_server(cfg, serve_http=True)
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(transport_security=sec)

    if not missing_auth_variables(cfg):
        logger.info("HTTP Basic Auth enabled for user '%s'", cfg.auth_user)
        app = BasicAuthMiddleware(app, cfg.auth_user, cfg.auth_pass)
    return app


def run_server(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Run MCP server with uvicorn."""
    cfg = _normalize_config(config, **kwargs)
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=logger.getEffectiveLevel())


def main():
    parser = argparse.ArgumentParser(description="Run the Seller Warrant MCP server over Streamable HTTP.")
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
        "--private-key",
        dest="seller_private_key",
        default=None,
        help="Private key for seller account to sign transactions (or set WARRANT_SELLER_KEY / WARRANT_PRIVATE_KEY)",
    )
    parser.add_argument(
        "--key-file",
        dest="seller_key_path",
        type=pathlib.Path,
        default=None,
        help="Path to file containing seller private key (default: from manifest or .keys/seller.key)",
    )
    args = parser.parse_args()

    config = ServerConfig(**vars(args))
    try:
        run_server(config)
    except StartRefusedError as exc:
        logger.error("seller-mcp-server: refused to start: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
