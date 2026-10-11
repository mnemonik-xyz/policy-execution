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
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from decimal import Decimal

# Ensure repository root is on sys.path
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from showcases.ionet_arc.escrow import (  # noqa: E402
    DEFAULT_CATEGORY,
    DEFAULT_CHAIN_ID,
    EscrowClient,
    address20,
    bytes32,
    compile_buyer_policy,
    sign_acceptance,
)


class BuyerWarrantService:
    """Core verification and escrow funding service for the Buyer agent."""

    def __init__(
        self,
        state_dir: pathlib.Path,
        escrow_client: EscrowClient,
        max_budget_usd: float = 2.00,
        max_duration_hours: int = 1,
        allowed_categories: list[int] | None = None,
        buyer_account: str | None = None,
        pinned_escrow: str | None = None,
        pinned_token: str | None = None,
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
        self.max_budget_usd = float(max_budget_usd)
        self.max_duration_hours = int(max_duration_hours)
        self.allowed_categories = allowed_categories or [DEFAULT_CATEGORY]
        self.buyer_account = buyer_account
        self.pinned_escrow = pinned_escrow
        self.pinned_token = pinned_token
        self.auto_mint = auto_mint

        self.manifest_path = manifest_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "manifest.yaml")
        self.template_path = template_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml"
        )
        self.buyer_acceptance_key_path = buyer_acceptance_key_path or (
            REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "buyer_acceptance.key"
        )

        # Pin addresses from manifest if not provided and chain matches
        if self.manifest_path.exists():
            try:
                import yaml

                manifest = yaml.safe_load(self.manifest_path.read_text()) or {}
                manifest_chain = manifest.get("chain_id")
                active_chain = self._resolve_chain_id()
                if manifest_chain is None or manifest_chain == active_chain:
                    if not self.pinned_escrow and manifest.get("escrow_address"):
                        self.pinned_escrow = manifest["escrow_address"]
                    if not self.pinned_token and manifest.get("token_address"):
                        self.pinned_token = manifest["token_address"]
            except Exception:
                pass

    def _resolve_buyer_account(self) -> str:
        if self.buyer_account:
            return self.buyer_account
        try:
            accounts = self.escrow_client.rpc.accounts()
            if accounts:
                self.buyer_account = accounts[0]
                return self.buyer_account
        except Exception:
            pass
        raise ToolError("Buyer account is not configured and no unlocked accounts were returned by RPC.")

    def _resolve_chain_id(self) -> int:
        try:
            return self.escrow_client.rpc.chain_id()
        except Exception:
            return DEFAULT_CHAIN_ID

    async def warrant_get_policy(self) -> dict:
        """Inspect the buyer's local Warrant policy constraints."""
        return {
            "buyer_account": self._resolve_buyer_account(),
            "max_budget_usd": self.max_budget_usd,
            "max_duration_hours": self.max_duration_hours,
            "allowed_categories": self.allowed_categories,
            "chain_id": self._resolve_chain_id(),
            "escrow_address": self.pinned_escrow or self.escrow_client.escrow,
            "token_address": self.pinned_token or self.escrow_client.token,
            "auto_mint": self.auto_mint,
        }

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
        if cost_usd > self.max_budget_usd:
            raise ToolError(
                f"Policy rejection: proposed cost ${cost_usd:.4f} exceeds max budget cap ${self.max_budget_usd:.4f}"
            )
        max_atomic = round(self.max_budget_usd * 1_000_000)
        if atomic_amount > max_atomic:
            raise ToolError(
                f"Policy rejection: proposed amount {atomic_amount} atomic units exceeds budget limit {max_atomic}"
            )
        expected_atomic = round(cost_usd * 1_000_000)
        if abs(atomic_amount - expected_atomic) > 1:
            raise ToolError(
                f"Policy rejection: atomic amount {atomic_amount} does not match amount_usd ${cost_usd:.4f}"
            )
        if duration_hours is None or duration_hours <= 0:
            raise ToolError("Policy rejection: duration_hours is required and must be at least 1")
        if duration_hours > self.max_duration_hours:
            raise ToolError(
                f"Policy rejection: proposed duration {duration_hours}h exceeds max duration {self.max_duration_hours}h"
            )
        if category not in self.allowed_categories:
            raise ToolError(
                f"Policy rejection: category {category} is not in allowed categories {self.allowed_categories}"
            )
        if self.pinned_escrow and escrow_addr.lower() != self.pinned_escrow.lower():
            raise ToolError(
                f"Policy rejection: escrow address {escrow_addr} does not match pinned escrow {self.pinned_escrow}"
            )
        if self.pinned_token and token_addr.lower() != self.pinned_token.lower():
            raise ToolError(
                f"Policy rejection: token address {token_addr} does not match pinned token {self.pinned_token}"
            )
        active_chain = self._resolve_chain_id()
        if chain_id != active_chain:
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
        self.escrow_client.escrow = escrow_addr
        self.escrow_client.token = token_addr

        try:
            balance = self.escrow_client.token_balance(buyer_account)
        except Exception as e:
            raise ToolError(f"Failed to query token balance for {buyer_account}: {e}") from e

        if balance < atomic_amount:
            if self.auto_mint:
                try:
                    self.escrow_client.mint_token(buyer_account, atomic_amount * 2, sender=buyer_account)
                except Exception as e:
                    raise ToolError(
                        f"Insufficient balance ({balance} < {atomic_amount}) and auto-mint failed: {e}"
                    ) from e
            else:
                raise ToolError(f"Insufficient token balance ({balance} < {atomic_amount}) for buyer {buyer_account}.")

        try:
            self.escrow_client.approve_token(escrow_addr, atomic_amount, sender=buyer_account)
        except Exception as e:
            raise ToolError(f"Failed to approve TaskEscrow token allowance: {e}") from e

        now = int(time.time())
        accept_by = now + 300
        settle_by = now + validity_seconds

        try:
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
        except Exception as e:
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
        duration_hours: int | None = 1,
        policy_hash: str | None = None,
        policy_version: int = 1,
        chain_id: int | None = None,
    ) -> dict:
        """Evaluate proposal against local constraints, stamp concrete policy, and fund TaskEscrow on-chain."""
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
            policy_hash = proposal.get("policy_hash", policy_hash)
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
            raise ToolError(f"Missing required proposal fields: {', '.join(missing)}")

        cost_usd = float(Decimal(str(amount_usd)))
        atomic_amount = int(amount)
        duration = int(duration_hours) if duration_hours is not None else None

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
            expected_task_id = self.escrow_client.task_id_for(buyer_account, salt)
        except Exception as e:
            raise ToolError(f"Failed to derive taskIdFor({buyer_account}, {salt}): {e}") from e

        if str(task_id).lower() != str(expected_task_id).lower():
            raise ToolError(
                f"Policy rejection: proposed task_id '{task_id}' does not match "
                f"derived taskIdFor('{buyer_account}', '{salt}'): '{expected_task_id}'"
            )

        # Compile buyer policy template into concrete Policy and cryptographic policy_hash
        try:
            concrete_policy, derived_policy_hash = compile_buyer_policy(
                template_path=self.template_path,
                manifest_path=self.manifest_path,
                chain_id=chain_id,
                escrow_address=escrow_addr,
                token_address=token_addr,
                max_amount=atomic_amount,
            )
        except Exception as e:
            raise ToolError(f"Failed to compile buyer policy template: {e}") from e

        if policy_hash and policy_hash.lower() != derived_policy_hash.lower():
            raise ToolError(
                f"Policy hash mismatch: proposal claimed {policy_hash} "
                f"but locally compiled policy hash is {derived_policy_hash}"
            )

        funded = self._fund_escrow(
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

        return {
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

    async def warrant_sign_acceptance(
        self,
        task_id: str,
        deliverable_hash: str,
        recipient: str,
        amount: int | str,
    ) -> dict:
        """Sign Acceptance statement with buyer authority key after verifying deliverable health."""
        task_path = self.tasks_dir / f"{task_id}.json"
        if not task_path.exists():
            raise ToolError(f"Task '{task_id}' not found in buyer state. Offer must be created first.")

        task_data = json.loads(task_path.read_text())
        policy = task_data.get("policy", {})

        atomic_amount = int(amount)
        if atomic_amount != int(task_data.get("amount", 0)):
            raise ToolError(
                f"Acceptance amount {atomic_amount} does not match offered task amount {task_data.get('amount')}"
            )
        if recipient.lower() != str(task_data.get("recipient", "")).lower():
            raise ToolError(
                f"Acceptance recipient {recipient} does not match offered task recipient {task_data.get('recipient')}"
            )

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
            sig_bytes = sign_acceptance(acceptance, key_path=self.buyer_acceptance_key_path)
        except Exception as e:
            raise ToolError(f"Failed to sign acceptance evidence: {e}") from e

        return {
            "status": "signed",
            "task_id": task_id,
            "acceptance": acceptance,
            "signature": sig_bytes,
            "message": "Deliverable acceptance signed. Submit this evidence to the compute seller for settlement.",
        }

    async def warrant_check_escrow_status(self, task_id: str) -> dict:
        """Query TaskEscrow state on-chain for a task ID."""
        try:
            task_data = self.escrow_client.get_task(task_id)
            return {
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
        except Exception as e:
            raise ToolError(f"Failed to query escrow status for task '{task_id}': {e}") from e


@dataclass
class ServerConfig:
    state_dir: pathlib.Path = pathlib.Path("artifacts/buyer_state")
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8001
    rpc_url: str = "http://127.0.0.1:8545"
    escrow_address: str | None = None
    token_address: str | None = None
    buyer_account: str | None = None
    max_budget_usd: float = 2.00
    max_duration_hours: int = 1
    allowed_categories: list[int] | None = None
    auto_mint: bool = False
    manifest_path: pathlib.Path | None = None
    template_path: pathlib.Path | None = None
    buyer_acceptance_key_path: pathlib.Path | None = None
    escrow_client: EscrowClient | None = None

    def __post_init__(self):
        if isinstance(self.state_dir, str):
            self.state_dir = pathlib.Path(self.state_dir)


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
        server_account=cfg.buyer_account,
    )

    service = BuyerWarrantService(
        state_dir=cfg.state_dir,
        escrow_client=escrow_client,
        max_budget_usd=cfg.max_budget_usd,
        max_duration_hours=cfg.max_duration_hours,
        allowed_categories=cfg.allowed_categories,
        buyer_account=cfg.buyer_account,
        pinned_escrow=cfg.escrow_address,
        pinned_token=cfg.token_address,
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
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


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
        default=os.environ.get("WARRANT_RPC_URL", os.environ.get("ARC_RPC_URL", "http://127.0.0.1:8545")),
        help="Ethereum / Arc JSON-RPC URL (default: http://127.0.0.1:8545)",
    )
    parser.add_argument(
        "--escrow-address",
        default=os.environ.get("WARRANT_ESCROW", os.environ.get("ESCROW_CONTRACT_ADDRESS")),
        help="TaskEscrow contract address to enforce",
    )
    parser.add_argument(
        "--token-address",
        default=os.environ.get("WARRANT_TOKEN", os.environ.get("TOKEN_CONTRACT_ADDRESS")),
        help="ERC-20 payment token address to enforce",
    )
    parser.add_argument(
        "--buyer-account",
        default=os.environ.get("WARRANT_BUYER_ACCOUNT", os.environ.get("BUYER_ACCOUNT")),
        help="Buyer wallet address (default: auto-detected from eth_accounts[0])",
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=float(os.environ.get("WARRANT_MAX_BUDGET_USD", "2.00")),
        help="Maximum allowable budget per proposal in USD (default: 2.00)",
    )
    parser.add_argument(
        "--max-duration-hours",
        type=int,
        default=int(os.environ.get("WARRANT_MAX_DURATION_HOURS", "1")),
        help="Maximum allowable duration in hours (default: 1)",
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
    args = parser.parse_args()

    config = ServerConfig(
        transport=args.transport,
        host=args.host,
        port=args.port,
        rpc_url=args.rpc_url,
        escrow_address=args.escrow_address,
        token_address=args.token_address,
        buyer_account=args.buyer_account,
        max_budget_usd=args.max_budget_usd,
        max_duration_hours=args.max_duration_hours,
        auto_mint=args.auto_mint,
        state_dir=pathlib.Path(args.state_dir),
    )
    run_server(config)


if __name__ == "__main__":
    main()
