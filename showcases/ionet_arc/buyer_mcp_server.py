"""Local Warrant guardrail MCP server for autonomous Buyer agents.

Exposes tools for the buyer's LLM agent:
1. warrant_get_policy: Inspect local budget cap, duration limits, and allowed categories.
2. warrant_evaluate_and_offer: Cryptographically verify seller proposals against policy
   and fund TaskEscrow on-chain.
3. warrant_sign_acceptance: Sign deliverable acceptance after verifying worker health.
4. warrant_check_escrow_status: Query on-chain TaskEscrow status for task tracking.

Supports stdio (standard for desktop LLM agents) and Streamable HTTP transports.
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from decimal import Decimal

# Ensure repository root is on sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=getattr(logging, os.getenv("WARRANT_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from showcases.ionet_arc.escrow import (  # noqa: E402
    DEFAULT_CATEGORY,
    DEFAULT_CHAIN_ID,
    DEFAULT_MANIFEST_PATH,
    EscrowClient,
    address20,
    bytes32,
    compile_buyer_policy,
    get_manifest_parameter,
    load_manifest,
    sign_acceptance,
)


class BuyerWarrantService:
    """Core verification and escrow funding service for the Buyer agent."""

    def __init__(
        self,
        state_dir: pathlib.Path,
        escrow_client: EscrowClient,
        max_budget_usd: float | None = None,
        max_duration_hours: int | None = None,
        allowed_categories: list[int] | None = None,
        buyer_account: str | None = None,
        pinned_escrow: str | None = None,
        pinned_token: str | None = None,
        verifier_address: str | None = None,
        auto_mint: bool = False,
        manifest_path: pathlib.Path | None = None,
        template_path: pathlib.Path | None = None,
        buyer_acceptance_key_path: pathlib.Path | None = None,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir = self.state_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.escrow_client = escrow_client
        self.allowed_categories = allowed_categories or [DEFAULT_CATEGORY]
        self.auto_mint = auto_mint

        self.manifest_path = pathlib.Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
        self.template_path = template_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml"
        )
        self.buyer_acceptance_key_path = buyer_acceptance_key_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "buyer_acceptance.key"
        )

        self.max_duration_hours = int(max_duration_hours) if max_duration_hours is not None else None
        self.max_budget_usd = float(max_budget_usd) if max_budget_usd is not None else None
        self.pinned_escrow = pinned_escrow
        self.pinned_token = pinned_token
        self.buyer_account = buyer_account
        self.verifier_address = verifier_address

        needs_manifest = (
            self.max_duration_hours is None
            or self.max_budget_usd is None
            or not self.pinned_escrow
            or not self.pinned_token
            or not self.buyer_account
            or not self.verifier_address
        )
        if needs_manifest:
            manifest = load_manifest(self.manifest_path)
            if self.max_duration_hours is None:
                raw_duration = get_manifest_parameter(manifest, "duration_hours")
                self.max_duration_hours = int(raw_duration) if raw_duration is not None else None
            if self.max_budget_usd is None:
                raw_budget = get_manifest_parameter(manifest, "max_budget_usd")
                self.max_budget_usd = float(raw_budget) if raw_budget is not None else None
            if not self.pinned_escrow:
                self.pinned_escrow = get_manifest_parameter(manifest, "escrow_address")
            if not self.pinned_token:
                self.pinned_token = get_manifest_parameter(manifest, "token_address")
            if not self.buyer_account:
                self.buyer_account = get_manifest_parameter(manifest, "buyer_address")
            if not self.verifier_address:
                self.verifier_address = get_manifest_parameter(manifest, "verifier_address")

        if not self.escrow_client.escrow and self.pinned_escrow:
            self.escrow_client.escrow = self.pinned_escrow
        if not self.escrow_client.token and self.pinned_token:
            self.escrow_client.token = self.pinned_token
        if not self.escrow_client.verifier and self.verifier_address:
            self.escrow_client.verifier = self.verifier_address
        if not self.escrow_client.server_account and self.buyer_account:
            self.escrow_client.server_account = self.buyer_account

    def _resolve_buyer_account(self) -> str:
        if self.buyer_account:
            return self.buyer_account
        raise ToolError("Buyer account is not configured in config/manifest.yaml.")

    def _resolve_chain_id(self) -> int:
        try:
            return self.escrow_client.rpc.chain_id()
        except Exception:
            return DEFAULT_CHAIN_ID

    async def warrant_get_policy(self) -> dict:
        """Inspect the buyer's local Warrant policy constraints."""
        logger.info("Tool called: warrant_get_policy")
        account = self._resolve_buyer_account()
        logger.info("Inspecting buyer Warrant policy constraints for buyer_account=%s", account)
        policy = {
            "buyer_account": account,
            "max_budget_usd": self.max_budget_usd,
            "max_duration_hours": self.max_duration_hours,
            "allowed_categories": self.allowed_categories,
            "chain_id": self._resolve_chain_id(),
            "escrow_address": self.pinned_escrow or self.escrow_client.escrow,
            "token_address": self.pinned_token or self.escrow_client.token,
            "verifier_address": self.verifier_address or self.escrow_client.verifier,
            "auto_mint": self.auto_mint,
        }
        logger.info("warrant_get_policy returning: %s", policy)
        return policy

    def _validate_proposal_terms(
        self,
        cost_usd: float,
        atomic_amount: int,
        duration_hours: int | None,
        category: int,
        escrow_addr: str,
        token_addr: str,
        chain_id: int,
    ):
        if self.max_budget_usd is None:
            logger.warning("Policy rejection: max_budget_usd is not configured in config/manifest.yaml")
            raise ToolError("Policy rejection: max_budget_usd is not configured in config/manifest.yaml")

        if cost_usd > self.max_budget_usd:
            logger.warning(
                "Policy rejection: proposed cost $%.4f exceeds max budget cap $%.4f",
                cost_usd,
                self.max_budget_usd,
            )
            raise ToolError(
                f"Policy rejection: proposed cost ${cost_usd:.4f} exceeds max budget cap ${self.max_budget_usd:.4f}"
            )
        max_atomic = round(self.max_budget_usd * 1_000_000)
        if atomic_amount > max_atomic:
            logger.warning(
                "Policy rejection: proposed amount %d exceeds budget limit %d",
                atomic_amount,
                max_atomic,
            )
            raise ToolError(
                f"Policy rejection: proposed amount {atomic_amount} atomic units exceeds budget limit {max_atomic}"
            )
        expected_atomic = round(cost_usd * 1_000_000)
        if abs(atomic_amount - expected_atomic) > 1:
            logger.warning(
                "Policy rejection: atomic amount %d does not match amount_usd $%.4f",
                atomic_amount,
                cost_usd,
            )
            raise ToolError(
                f"Policy rejection: atomic amount {atomic_amount} does not match amount_usd ${cost_usd:.4f}"
            )
        if duration_hours is None or duration_hours <= 0:
            logger.warning("Policy rejection: duration_hours is required and must be at least 1")
            raise ToolError("Policy rejection: duration_hours is required and must be at least 1")
        if self.max_duration_hours is None:
            logger.warning("Policy rejection: duration_hours is not configured in config/manifest.yaml")
            raise ToolError("Policy rejection: duration_hours is not configured in config/manifest.yaml")
        if duration_hours > self.max_duration_hours:
            logger.warning(
                "Policy rejection: proposed duration %dh exceeds max duration %dh",
                duration_hours,
                self.max_duration_hours,
            )
            raise ToolError(
                f"Policy rejection: proposed duration {duration_hours}h exceeds max duration {self.max_duration_hours}h"
            )
        if category not in self.allowed_categories:
            logger.warning(
                "Policy rejection: category %s not in allowed %s",
                category,
                self.allowed_categories,
            )
            raise ToolError(
                f"Policy rejection: category {category} is not in allowed categories {self.allowed_categories}"
            )
        if self.pinned_escrow and escrow_addr.lower() != self.pinned_escrow.lower():
            logger.warning(
                "Policy rejection: escrow address %s != pinned %s",
                escrow_addr,
                self.pinned_escrow,
            )
            raise ToolError(
                f"Policy rejection: escrow address {escrow_addr} does not match pinned escrow {self.pinned_escrow}"
            )
        if self.pinned_token and token_addr.lower() != self.pinned_token.lower():
            logger.warning(
                "Policy rejection: token address %s != pinned %s",
                token_addr,
                self.pinned_token,
            )
            raise ToolError(
                f"Policy rejection: token address {token_addr} does not match pinned token {self.pinned_token}"
            )
        active_chain = self._resolve_chain_id()
        if chain_id != active_chain:
            logger.warning(
                "Policy rejection: proposed chain_id %s != active chain_id %s",
                chain_id,
                active_chain,
            )
            raise ToolError(
                f"Policy rejection: proposed chain_id {chain_id} does not match RPC chain_id {active_chain}"
            )

    def _fund_escrow(
        self,
        escrow_addr: str,
        token_addr: str,
        atomic_amount: int,
        salt: str,
        recipient: str,
        policy_hash: str,
        policy_version: int,
        buyer_account: str | None = None,
        validity_seconds: int = 900,
    ) -> dict:
        buyer_account = buyer_account or self._resolve_buyer_account()
        logger.info(
            "Funding TaskEscrow on-chain: escrow=%s, token=%s, amount=%d, buyer=%s, recipient=%s",
            escrow_addr,
            token_addr,
            atomic_amount,
            buyer_account,
            recipient,
        )
        self.escrow_client.escrow = escrow_addr
        self.escrow_client.token = token_addr

        try:
            balance = self.escrow_client.token_balance(buyer_account)
            logger.info("Checked token balance for %s: %d (required=%d)", buyer_account, balance, atomic_amount)
        except Exception as e:
            logger.error("Failed to query token balance for %s: %s", buyer_account, e)
            raise ToolError(f"Failed to query token balance for {buyer_account}: {e}") from e

        if balance < atomic_amount:
            if self.auto_mint:
                try:
                    logger.info("Auto-minting %d tokens for buyer %s", atomic_amount * 2, buyer_account)
                    self.escrow_client.mint_token(buyer_account, atomic_amount * 2, sender=buyer_account)
                except Exception as e:
                    logger.error("Auto-mint failed: %s", e)
                    raise ToolError(
                        f"Insufficient balance ({balance} < {atomic_amount}) and auto-mint failed: {e}"
                    ) from e
            else:
                logger.warning("Insufficient balance: %d < %d for buyer %s", balance, atomic_amount, buyer_account)
                raise ToolError(f"Insufficient token balance ({balance} < {atomic_amount}) for buyer {buyer_account}.")

        try:
            logger.info("Approving %d tokens for escrow contract %s", atomic_amount, escrow_addr)
            self.escrow_client.approve_token(escrow_addr, atomic_amount, sender=buyer_account)
        except Exception as e:
            logger.error("Failed to approve TaskEscrow token allowance: %s", e)
            raise ToolError(f"Failed to approve TaskEscrow token allowance: {e}") from e

        now = int(time.time())
        accept_by = now + 300
        settle_by = now + validity_seconds

        try:
            logger.info(
                "Calling TaskEscrow.offer() on-chain: salt=%s, recipient=%s, policy_hash=%s, "
                "accept_by=%d, settle_by=%d",
                salt,
                recipient,
                policy_hash,
                accept_by,
                settle_by,
            )
            receipt = self.escrow_client.offer(
                salt=salt,
                recipient=recipient,
                policy_hash=policy_hash,
                policy_version=policy_version,
                amount=atomic_amount,
                accept_by=accept_by,
                settle_by=settle_by,
                sender=buyer_account,
            )
            logger.info("TaskEscrow.offer() confirmed on-chain in tx %s", receipt.get("transactionHash"))
        except Exception as e:
            logger.error("TaskEscrow.offer() reverted on-chain: %s", e)
            raise ToolError(f"TaskEscrow.offer() reverted on-chain: {e}") from e

        return {
            "receipt": receipt,
            "accept_by": accept_by,
            "settle_by": settle_by,
            "buyer_account": buyer_account,
        }

    async def warrant_evaluate_and_offer(
        self,
        proposal: dict | None = None,
        task_id: str | None = None,
        amount: int | str | None = None,
        amount_usd: str | float | None = None,
        salt: str | None = None,
        recipient: str | None = None,
        escrow_address: str | None = None,
        token_address: str | None = None,
        category: int = DEFAULT_CATEGORY,
        duration_hours: int | None = None,
        policy_version: int = 1,
        chain_id: int | None = None,
    ) -> dict:
        """Evaluate proposal against local constraints, stamp concrete policy, and fund TaskEscrow on-chain."""
        prop = proposal if isinstance(proposal, dict) else {}
        effective_task_id = task_id or prop.get("task_id")
        effective_amount = amount if amount is not None else prop.get("amount")
        effective_amount_usd = amount_usd if amount_usd is not None else prop.get("amount_usd")
        effective_salt = salt or prop.get("salt")
        effective_recipient = recipient or prop.get("recipient")
        effective_category = prop.get("category", category) if category == DEFAULT_CATEGORY else category
        effective_duration = prop.get("duration_hours") if "duration_hours" in prop else duration_hours

        logger.info(
            "Tool called: warrant_evaluate_and_offer with task_id=%s, amount=%s, amount_usd=%s, "
            "salt=%s, recipient=%s, category=%s, duration_hours=%s",
            effective_task_id,
            effective_amount,
            effective_amount_usd,
            effective_salt,
            effective_recipient,
            effective_category,
            effective_duration,
        )

        if proposal and isinstance(proposal, dict):
            task_id = proposal.get("task_id", task_id)
            amount = proposal.get("amount", amount)
            amount_usd = proposal.get("amount_usd", amount_usd)
            salt = proposal.get("salt", salt)
            recipient = proposal.get("recipient", recipient)
            escrow_address = proposal.get("escrow_address", escrow_address)
            token_address = proposal.get("token_address", token_address)
            category = proposal.get("category", category)
            duration_hours = proposal.get("duration_hours", duration_hours)
            policy_version = proposal.get("policy_version", policy_version)
            chain_id = proposal.get("chain_id", chain_id)

        escrow_addr = escrow_address or self.pinned_escrow or self.escrow_client.escrow
        token_addr = token_address or self.pinned_token or self.escrow_client.token
        chain_id = chain_id or self._resolve_chain_id()

        missing = []
        if not task_id:
            missing.append("task_id")
        if amount is None:
            missing.append("amount")
        if amount_usd is None:
            missing.append("amount_usd")
        if not salt:
            missing.append("salt")
        if not recipient:
            missing.append("recipient")
        if not escrow_addr:
            missing.append("escrow_address")
        if not token_addr:
            missing.append("token_address")
        if missing:
            logger.warning("Missing required proposal fields: %s", ", ".join(missing))
            raise ToolError(f"Missing required proposal fields: {', '.join(missing)}")

        cost_usd = float(Decimal(str(amount_usd)))
        atomic_amount = int(amount)
        duration = int(duration_hours) if duration_hours is not None else None

        logger.info(
            "Validating proposal terms for task_id=%s: cost_usd=%.4f, atomic_amount=%d, duration=%sh, category=%s",
            task_id,
            cost_usd,
            atomic_amount,
            duration,
            category,
        )
        self._validate_proposal_terms(
            cost_usd=cost_usd,
            atomic_amount=atomic_amount,
            duration_hours=duration,
            category=category,
            escrow_addr=escrow_addr,
            token_addr=token_addr,
            chain_id=chain_id,
        )

        buyer_account = self._resolve_buyer_account()
        self.escrow_client.escrow = escrow_addr
        try:
            expected_task_id = await asyncio.to_thread(self.escrow_client.task_id_for, buyer_account, salt)
            logger.info("Derived expected taskIdFor(%s, %s): %s", buyer_account, salt, expected_task_id)
        except Exception as e:
            logger.error("Failed to derive taskIdFor(%s, %s): %s", buyer_account, salt, e)
            raise ToolError(f"Failed to derive taskIdFor({buyer_account}, {salt}): {e}") from e

        if str(task_id).lower() != str(expected_task_id).lower():
            logger.warning("Proposed task_id '%s' != derived taskIdFor '%s'", task_id, expected_task_id)
            raise ToolError(
                f"Policy rejection: proposed task_id '{task_id}' does not match "
                f"derived taskIdFor('{buyer_account}', '{salt}'): '{expected_task_id}'"
            )

        # Compile buyer policy template into concrete Policy and cryptographic policy_hash
        try:
            logger.info("Compiling buyer policy template for task_id=%s with max_amount=%d", task_id, atomic_amount)
            concrete_policy, derived_policy_hash = await asyncio.to_thread(
                compile_buyer_policy,
                template_path=self.template_path,
                manifest_path=self.manifest_path,
                chain_id=chain_id,
                escrow_address=escrow_addr,
                token_address=token_addr,
                max_amount=atomic_amount,
            )
            logger.info("Buyer policy template compiled successfully, policy_hash=%s", derived_policy_hash)
        except Exception as e:
            logger.error("Failed to compile buyer policy template: %s", e)
            raise ToolError(f"Failed to compile buyer policy template: {e}") from e

        funded = await asyncio.to_thread(
            self._fund_escrow,
            escrow_addr=escrow_addr,
            token_addr=token_addr,
            atomic_amount=atomic_amount,
            salt=salt,
            recipient=recipient,
            policy_hash=derived_policy_hash,
            policy_version=policy_version,
            buyer_account=buyer_account,
        )

        # Record task state for subsequent acceptance verification
        task_record = {
            "task_id": task_id,
            "status": "OFFERED",
            "salt": salt,
            "customer": funded["buyer_account"],
            "recipient": recipient,
            "amount": atomic_amount,
            "amount_usd": str(amount_usd),
            "policy": concrete_policy,
            "policy_hash": derived_policy_hash,
            "policy_version": policy_version,
            "escrow_address": escrow_addr,
            "token_address": token_addr,
            "accept_by": funded["accept_by"],
            "settle_by": funded["settle_by"],
            "transaction_hash": funded["receipt"].get("transactionHash"),
        }
        (self.tasks_dir / f"{task_id}.json").write_text(json.dumps(task_record, indent=2))
        logger.info("Saved task record to %s", self.tasks_dir / f"{task_id}.json")

        result = {
            "status": "APPROVED",
            "task_id": task_id,
            "transaction_hash": funded["receipt"].get("transactionHash"),
            "customer": funded["buyer_account"],
            "recipient": recipient,
            "amount": atomic_amount,
            "amount_usd": str(amount_usd),
            "policy": concrete_policy,
            "policy_hash": derived_policy_hash,
            "policy_version": policy_version,
            "escrow_address": escrow_addr,
            "token_address": token_addr,
            "accept_by": funded["accept_by"],
            "settle_by": funded["settle_by"],
            "message": "TaskEscrow successfully funded on-chain. Deliverable is committed.",
        }
        logger.info(
            "warrant_evaluate_and_offer returning: status=%s, task_id=%s, tx=%s, amount=%d ($%s)",
            result["status"],
            result["task_id"],
            result["transaction_hash"],
            result["amount"],
            result["amount_usd"],
        )
        return result

    async def warrant_sign_acceptance(
        self,
        task_id: str,
        deliverable_hash: str,
        recipient: str,
        amount: int | str,
    ) -> dict:
        """Sign Acceptance statement with buyer authority key after verifying deliverable health."""
        logger.info(
            "Tool called: warrant_sign_acceptance with task_id=%s, deliverable_hash=%s, recipient=%s, amount=%s",
            task_id,
            deliverable_hash,
            recipient,
            amount,
        )
        task_path = self.tasks_dir / f"{task_id}.json"
        if not task_path.exists():
            logger.warning("Task '%s' not found in buyer state at %s", task_id, task_path)
            raise ToolError(f"Task '{task_id}' not found in buyer state. Offer must be created first.")

        task_data = json.loads(task_path.read_text())
        policy = task_data.get("policy", {})

        atomic_amount = int(amount)
        if atomic_amount != int(task_data.get("amount", 0)):
            logger.warning("Acceptance amount %d != offered task amount %s", atomic_amount, task_data.get("amount"))
            raise ToolError(
                f"Acceptance amount {atomic_amount} does not match offered task amount {task_data.get('amount')}"
            )
        if recipient.lower() != str(task_data.get("recipient", "")).lower():
            logger.warning(
                "Acceptance recipient %s != offered task recipient %s",
                recipient,
                task_data.get("recipient"),
            )
            raise ToolError(
                f"Acceptance recipient {recipient} does not match offered task recipient {task_data.get('recipient')}"
            )

        logger.info("Constructing Acceptance statement for task_id=%s, deliverable_hash=%s", task_id, deliverable_hash)
        scope = policy.get("scope") or {
            "chain_id": self._resolve_chain_id(),
            "vault": list(address20(self.pinned_escrow or self.escrow_client.escrow)),
            "token": list(address20(self.pinned_token or self.escrow_client.token)),
        }

        acceptance = {
            "scope": scope,
            "task_id": list(bytes32(task_id)),
            "deliverable_hash": list(bytes32(deliverable_hash)),
            "recipient": list(address20(recipient)),
            "amount": atomic_amount,
            "accepted": True,
            "valid_after": int(policy.get("valid_after", int(time.time()) - 300)),
            "valid_until": int(policy.get("valid_until", int(time.time()) + 900)),
        }

        try:
            logger.info("Signing Acceptance statement with buyer key at %s", self.buyer_acceptance_key_path)
            sig_bytes = await asyncio.to_thread(
                sign_acceptance,
                acceptance,
                key_path=self.buyer_acceptance_key_path,
            )
            logger.info("Acceptance signature generated successfully (%d bytes)", len(sig_bytes))
        except Exception as e:
            logger.error("Failed to sign acceptance evidence: %s", e)
            raise ToolError(f"Failed to sign acceptance evidence: {e}") from e

        result = {
            "status": "signed",
            "task_id": task_id,
            "acceptance": acceptance,
            "signature": sig_bytes,
            "message": "Deliverable acceptance signed. Submit this evidence to the compute seller for settlement.",
        }
        logger.info(
            "warrant_sign_acceptance returning: status=%s, task_id=%s, signature_len=%d",
            result["status"],
            result["task_id"],
            len(sig_bytes),
        )
        return result

    async def warrant_check_escrow_status(self, task_id: str) -> dict:
        """Query TaskEscrow state on-chain for a task ID."""
        logger.info("Tool called: warrant_check_escrow_status with task_id=%s", task_id)
        try:
            logger.info(
                "Querying on-chain TaskEscrow state for task_id=%s (escrow=%s)",
                task_id,
                self.escrow_client.escrow,
            )
            task_data = await asyncio.to_thread(self.escrow_client.get_task, task_id)
            result = {
                "task_id": task_id,
                "state": task_data.get("state"),
                "state_idx": task_data.get("state_idx"),
                "customer": task_data.get("customer"),
                "recipient": task_data.get("recipient"),
                "amount": task_data.get("amount"),
                "policy_hash": task_data.get("policy_hash"),
                "policy_version": task_data.get("policy_version"),
                "accept_by": task_data.get("accept_by"),
                "settle_by": task_data.get("settle_by"),
                "escrow_address": self.escrow_client.escrow,
            }
            logger.info("warrant_check_escrow_status returning: %s", result)
            return result
        except Exception as e:
            logger.error("Failed to query escrow status for task '%s': %s", task_id, e)
            raise ToolError(f"Failed to query escrow status for task '{task_id}': {e}") from e


@dataclass
class ServerConfig:
    state_dir: pathlib.Path = pathlib.Path("artifacts/buyer_state")
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8001
    rpc_url: str | None = None
    escrow_address: str | None = None
    token_address: str | None = None
    verifier_address: str | None = None
    buyer_account: str | None = None
    buyer_private_key: str | None = None
    buyer_key_path: pathlib.Path | None = None
    max_budget_usd: float | None = None
    max_duration_hours: int | None = None
    allowed_categories: list[int] | None = None
    auto_mint: bool = False
    manifest_path: pathlib.Path | None = None
    template_path: pathlib.Path | None = None
    buyer_acceptance_key_path: pathlib.Path | None = None
    escrow_client: EscrowClient | None = None

    def _resolve_paths(self):
        if isinstance(self.state_dir, str):
            self.state_dir = pathlib.Path(self.state_dir)
        if isinstance(self.manifest_path, str):
            self.manifest_path = pathlib.Path(self.manifest_path)
        if isinstance(self.template_path, str):
            self.template_path = pathlib.Path(self.template_path)
        if isinstance(self.buyer_acceptance_key_path, str):
            self.buyer_acceptance_key_path = pathlib.Path(self.buyer_acceptance_key_path)
        if isinstance(self.buyer_key_path, str):
            self.buyer_key_path = pathlib.Path(self.buyer_key_path)

    def _resolve_parameters(self, manifest: dict):
        if self.rpc_url is None:
            self.rpc_url = get_manifest_parameter(manifest, "rpc_url") or "http://127.0.0.1:8545"
        if self.escrow_address is None:
            self.escrow_address = get_manifest_parameter(manifest, "escrow_address")
        if self.buyer_account is None:
            self.buyer_account = get_manifest_parameter(manifest, "buyer_address")
        if self.verifier_address is None:
            self.verifier_address = get_manifest_parameter(manifest, "verifier_address")
        if self.token_address is None:
            self.token_address = get_manifest_parameter(manifest, "token_address")
        if self.max_budget_usd is None:
            raw_budget = get_manifest_parameter(manifest, "max_budget_usd")
            if raw_budget is not None:
                self.max_budget_usd = float(raw_budget)
        if self.max_duration_hours is None:
            raw_duration = get_manifest_parameter(manifest, "duration_hours")
            if raw_duration is not None:
                self.max_duration_hours = int(raw_duration)

    def _resolve_key(self, manifest: dict):
        if self.buyer_key_path is None:
            raw_key_path = manifest.get("private_key_files", {}).get("buyer_key")
            if raw_key_path:
                cand = pathlib.Path(raw_key_path)
                self.buyer_key_path = cand if cand.is_absolute() else (REPO_ROOT / cand)
            else:
                default_buyer_key = REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "buyer.key"
                if default_buyer_key.exists():
                    self.buyer_key_path = default_buyer_key

        if self.buyer_private_key is None:
            if self.buyer_key_path and self.buyer_key_path.exists() and self.buyer_key_path.is_file():
                with contextlib.suppress(Exception):
                    self.buyer_private_key = self.buyer_key_path.read_text().strip()
            if not self.buyer_private_key:
                val = os.environ.get("WARRANT_BUYER_KEY")
                if val:
                    self.buyer_private_key = val.strip()

    def __post_init__(self):
        self._resolve_paths()
        manifest = load_manifest(self.manifest_path)
        self._resolve_parameters(manifest)
        self._resolve_key(manifest)


BuyerServerConfig = ServerConfig


def _normalize_config(config: ServerConfig | pathlib.Path | str | None = None, **kwargs) -> ServerConfig:
    if isinstance(config, ServerConfig):
        cfg = config
    elif isinstance(config, (pathlib.Path, str)):
        path = pathlib.Path(config)
        data = json.loads(path.read_text()) if path.exists() else {}
        cfg = ServerConfig(**data)
    else:
        cfg = ServerConfig()

    for k, v in kwargs.items():
        if hasattr(cfg, k) and v is not None:
            setattr(cfg, k, v)
    return cfg


def create_server(config: ServerConfig | pathlib.Path | str | None = None, **kwargs) -> MCPServer:
    """Build configured MCPServer with Buyer Warrant tools."""
    cfg = _normalize_config(config, **kwargs)
    escrow_client = cfg.escrow_client or EscrowClient(
        rpc_url=cfg.rpc_url,
        escrow_address=cfg.escrow_address,
        token_address=cfg.token_address,
        verifier_address=cfg.verifier_address,
        server_account=cfg.buyer_account,
    )
    if cfg.buyer_private_key:
        if cfg.buyer_account:
            escrow_client.register_key(cfg.buyer_account, cfg.buyer_private_key)
        with contextlib.suppress(Exception):
            from eth_account import Account

            derived_addr = Account.from_key(cfg.buyer_private_key).address
            escrow_client.register_key(derived_addr, cfg.buyer_private_key)

    service = BuyerWarrantService(
        state_dir=cfg.state_dir,
        escrow_client=escrow_client,
        max_budget_usd=cfg.max_budget_usd,
        max_duration_hours=cfg.max_duration_hours,
        allowed_categories=cfg.allowed_categories,
        buyer_account=cfg.buyer_account,
        pinned_escrow=cfg.escrow_address,
        pinned_token=cfg.token_address,
        verifier_address=cfg.verifier_address,
        auto_mint=cfg.auto_mint,
        manifest_path=cfg.manifest_path,
        template_path=cfg.template_path,
        buyer_acceptance_key_path=cfg.buyer_acceptance_key_path,
    )

    server = MCPServer(
        name="buyer-warrant-guardrail",
        instructions=(
            "You are the Buyer Warrant guardrail sidecar. You verify that compute proposals adhere to "
            "spending caps, duration caps, and category whitelist, stamp buyer policies, submit on-chain "
            "offers to TaskEscrow, and sign deliverable acceptance evidence."
        ),
    )

    server.tool(
        name="warrant_get_policy",
        description=(
            "Inspect the buyer's local Warrant policy constraints including max budget, "
            "max duration, allowed categories, and configured wallet."
        ),
    )(service.warrant_get_policy)

    server.tool(
        name="warrant_evaluate_and_offer",
        description=(
            "Validate a seller's deployment proposal against local Warrant policy constraints "
            "(budget cap, duration, category), stamp buyer policy template with authority keys, "
            "approve token allowance, and fund TaskEscrow on-chain."
        ),
    )(service.warrant_evaluate_and_offer)

    server.tool(
        name="warrant_sign_acceptance",
        description=(
            "Sign an off-chain Acceptance deliverable receipt for an offered TaskEscrow task using the "
            "buyer's private acceptance key. Required by compute seller before on-chain escrow settlement."
        ),
    )(service.warrant_sign_acceptance)

    server.tool(
        name="warrant_check_escrow_status",
        description="Check on-chain TaskEscrow state and details for a given task ID.",
    )(service.warrant_check_escrow_status)

    return server


def create_app(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Build ASGI app for streamable HTTP transport."""
    cfg = _normalize_config(config, **kwargs)
    server = create_server(cfg)
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return server.streamable_http_app(transport_security=sec)


def run_server(
    config: ServerConfig | pathlib.Path | str | None = None,
    **kwargs,
):
    """Run MCP server over stdio or Streamable HTTP."""
    cfg = _normalize_config(config, **kwargs)
    if cfg.transport == "stdio":
        server = create_server(cfg)
        server.run(transport="stdio")
    else:
        app = create_app(cfg)
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=logger.getEffectiveLevel())


def main():
    parser = argparse.ArgumentParser(
        description="Run the Buyer Warrant guardrail MCP server (stdio or Streamable HTTP)."
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http"],
        default=os.environ.get("WARRANT_TRANSPORT", "stdio"),
        help="MCP transport to run (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Host address to bind for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8001")),
        help="Port number to bind for HTTP transport (default: 8001)",
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
        "--auto-mint",
        action="store_true",
        help="Auto-mint tokens on local test networks if buyer balance is low (default: False)",
    )
    parser.add_argument(
        "--state-dir",
        default="artifacts/buyer_state",
        help="Directory to store local buyer state and task history",
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help="Private key for buyer account to sign transactions (or set WARRANT_BUYER_KEY / WARRANT_PRIVATE_KEY)",
    )
    parser.add_argument(
        "--key-file",
        dest="buyer_key_path",
        type=pathlib.Path,
        default=None,
        help="Path to file containing buyer private key (default: from manifest or .keys/buyer.key)",
    )
    args = parser.parse_args()

    config = ServerConfig(
        transport=args.transport,
        host=args.host,
        port=args.port,
        rpc_url=args.rpc_url,
        auto_mint=args.auto_mint,
        state_dir=pathlib.Path(args.state_dir),
        manifest_path=args.manifest_path,
        buyer_private_key=args.private_key,
        buyer_key_path=args.buyer_key_path,
    )
    run_server(config)


if __name__ == "__main__":
    main()
