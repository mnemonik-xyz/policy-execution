"""Local Warrant guardrail MCP server for autonomous Buyer agents.

Exposes tools for the buyer's LLM agent:
1. warrant_get_policy: Inspect local budget cap, duration limits, and allowed categories.
2. warrant_evaluate_and_offer: Cryptographically verify seller proposals against policy
   and fund TaskEscrow on-chain.
3. warrant_check_escrow_status: Query on-chain TaskEscrow status for task tracking.

Supports stdio (standard for desktop LLM agents) and Streamable HTTP transports.
"""
import argparse
import json
import os
import pathlib
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from showcases.ionet_arc.escrow import (
    DEFAULT_CATEGORY,
    DEFAULT_CHAIN_ID,
    EscrowClient,
    ensure_policy_hash,
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
        auto_mint: bool = True,
    ):
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.escrow_client = escrow_client
        self.max_budget_usd = float(max_budget_usd)
        self.max_duration_hours = int(max_duration_hours)
        self.allowed_categories = allowed_categories or [DEFAULT_CATEGORY]
        self.buyer_account = buyer_account
        self.pinned_escrow = pinned_escrow
        self.pinned_token = pinned_token
        self.auto_mint = auto_mint

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
        return "0x" + "11" * 20

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
        duration_hours: int | None,
        category: int,
        escrow_addr: str,
        token_addr: str,
    ):
        if cost_usd > self.max_budget_usd:
            raise ToolError(
                f"Policy rejection: proposed cost ${cost_usd:.4f} exceeds max budget cap ${self.max_budget_usd:.4f}"
            )
        if duration_hours is not None and duration_hours > self.max_duration_hours:
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

    def _verify_policy_hash(
        self,
        escrow_addr: str,
        token_addr: str,
        chain_id: int,
        category: int,
        claimed_hash: str | None,
    ) -> str:
        expected_hash = ensure_policy_hash(
            escrow_address=escrow_addr,
            token_address=token_addr,
            chain_id=chain_id,
            max_amount=100_000_000,
            state_dir=self.state_dir,
            categories=[category],
        )
        if claimed_hash and claimed_hash.lower() != expected_hash.lower():
            raise ToolError(
                f"Policy hash mismatch: proposal claimed {claimed_hash} but locally derived hash is {expected_hash}"
            )
        return expected_hash

    def _fund_escrow(
        self,
        escrow_addr: str,
        token_addr: str,
        atomic_amount: int,
        salt: str,
        recipient: str,
        policy_hash: str,
        policy_version: int,
    ) -> dict:
        buyer_account = self._resolve_buyer_account()
        self.escrow_client.escrow = escrow_addr
        self.escrow_client.token = token_addr

        try:
            balance = self.escrow_client.token_balance(buyer_account)
        except Exception as e:
            raise ToolError(f"Failed to query token balance for {buyer_account}: {e}") from e

        if balance < atomic_amount:
            if self.auto_mint:
                try:
                    self.escrow_client.mint_token(buyer_account, atomic_amount * 10, sender=buyer_account)
                except Exception as e:
                    raise ToolError(
                        f"Insufficient balance ({balance} < {atomic_amount}) and auto-mint failed: {e}"
                    ) from e
            else:
                raise ToolError(
                    f"Insufficient token balance ({balance} < {atomic_amount}) for buyer {buyer_account}."
                )

        try:
            self.escrow_client.approve_token(escrow_addr, atomic_amount, sender=buyer_account)
        except Exception as e:
            raise ToolError(f"Failed to approve TaskEscrow token allowance: {e}") from e

        now = int(time.time())
        accept_by = now + 3600
        settle_by = now + 86400
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
            raise ToolError(f"Failed to submit TaskEscrow.offer() on-chain: {e}") from e

        return {
            "receipt": receipt,
            "accept_by": accept_by,
            "settle_by": settle_by,
            "buyer_account": buyer_account,
        }

    def _parse_proposal_dict(self, proposal: dict | str | None) -> dict:
        if isinstance(proposal, str):
            try:
                return json.loads(proposal)
            except Exception as e:
                raise ToolError(f"Invalid proposal JSON: {e}") from e
        if isinstance(proposal, dict):
            return dict(proposal)
        return {}

    async def warrant_evaluate_and_offer(
        self,
        proposal: dict | str | None = None,
        task_id: str | None = None,
        amount: int | str | None = None,
        amount_usd: float | str | None = None,
        duration_hours: int | None = None,
        salt: str | None = None,
        recipient: str | None = None,
        policy_hash: str | None = None,
        policy_version: int = 1,
        category: int = DEFAULT_CATEGORY,
        escrow_address: str | None = None,
        token_address: str | None = None,
    ) -> dict:
        """Validate proposal against Warrant policy and fund TaskEscrow on-chain."""
        prop = self._parse_proposal_dict(proposal)

        task_id = prop.get("task_id") or task_id
        amount = prop.get("amount") if prop.get("amount") is not None else amount
        amount_usd = prop.get("amount_usd") if prop.get("amount_usd") is not None else amount_usd
        duration_hours = prop.get("duration_hours") if prop.get("duration_hours") is not None else duration_hours
        salt = prop.get("salt") or salt
        recipient = prop.get("recipient") or recipient
        policy_hash = prop.get("policy_hash") or policy_hash
        policy_version = int(prop.get("policy_version") or policy_version or 1)
        category = int(prop.get("category") or category or DEFAULT_CATEGORY)
        escrow_addr = prop.get("escrow_address") or escrow_address or self.pinned_escrow or self.escrow_client.escrow
        token_addr = prop.get("token_address") or token_address or self.pinned_token or self.escrow_client.token

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

        cost_usd = float(amount_usd)
        atomic_amount = int(amount)
        duration = int(duration_hours) if duration_hours is not None else None

        self._validate_proposal_terms(cost_usd, duration, category, escrow_addr, token_addr)
        chain_id = self._resolve_chain_id()
        expected_hash = self._verify_policy_hash(escrow_addr, token_addr, chain_id, category, policy_hash)

        funded = self._fund_escrow(
            escrow_addr=escrow_addr,
            token_addr=token_addr,
            atomic_amount=atomic_amount,
            salt=salt,
            recipient=recipient,
            policy_hash=expected_hash,
            policy_version=policy_version,
        )

        return {
            "status": "APPROVED_AND_OFFERED",
            "task_id": task_id,
            "transaction_hash": funded["receipt"].get("transactionHash"),
            "customer": funded["buyer_account"],
            "recipient": recipient,
            "amount": atomic_amount,
            "amount_usd": str(amount_usd),
            "policy_hash": expected_hash,
            "policy_version": policy_version,
            "escrow_address": escrow_addr,
            "token_address": token_addr,
            "accept_by": funded["accept_by"],
            "settle_by": funded["settle_by"],
            "message": "Task escrow successfully funded on-chain. Proceed to call seller's deploy_with_escrow tool.",
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


def create_server(
    state_dir: pathlib.Path,
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    buyer_account: str | None = None,
    max_budget_usd: float = 2.00,
    max_duration_hours: int = 1,
    allowed_categories: list[int] | None = None,
    auto_mint: bool = True,
    escrow_client: EscrowClient | None = None,
) -> MCPServer:
    """Instantiate and configure the Buyer Warrant MCPServer."""
    if escrow_client is None:
        escrow_client = EscrowClient(
            rpc_url=rpc_url,
            escrow_address=escrow_address,
            token_address=token_address,
            server_account=buyer_account,
        )

    service = BuyerWarrantService(
        state_dir=state_dir,
        escrow_client=escrow_client,
        max_budget_usd=max_budget_usd,
        max_duration_hours=max_duration_hours,
        allowed_categories=allowed_categories,
        buyer_account=buyer_account,
        pinned_escrow=escrow_address,
        pinned_token=token_address,
        auto_mint=auto_mint,
    )

    server = MCPServer("buyer-warrant-mcp-server")

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
            "(budget cap, duration, category, and independent cryptographic policy hash), "
            "approve token allowance, and fund the TaskEscrow contract on-chain."
        ),
    )(service.warrant_evaluate_and_offer)

    server.tool(
        name="warrant_check_escrow_status",
        description="Check on-chain TaskEscrow state and details for a given task ID.",
    )(service.warrant_check_escrow_status)

    return server


def create_app(
    state_dir: pathlib.Path,
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    buyer_account: str | None = None,
    max_budget_usd: float = 2.00,
    max_duration_hours: int = 1,
    allowed_categories: list[int] | None = None,
    auto_mint: bool = True,
    escrow_client: EscrowClient | None = None,
):
    """Build ASGI app for streamable HTTP transport."""
    server = create_server(
        state_dir=state_dir,
        rpc_url=rpc_url,
        escrow_address=escrow_address,
        token_address=token_address,
        buyer_account=buyer_account,
        max_budget_usd=max_budget_usd,
        max_duration_hours=max_duration_hours,
        allowed_categories=allowed_categories,
        auto_mint=auto_mint,
        escrow_client=escrow_client,
    )
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return server.streamable_http_app(transport_security=sec)


def run_server(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8001,
    state_dir: pathlib.Path = pathlib.Path("artifacts/buyer_state"),
    rpc_url: str = "http://127.0.0.1:8545",
    escrow_address: str | None = None,
    token_address: str | None = None,
    buyer_account: str | None = None,
    max_budget_usd: float = 2.00,
    max_duration_hours: int = 1,
    allowed_categories: list[int] | None = None,
    auto_mint: bool = True,
):
    """Run MCP server over stdio or Streamable HTTP."""
    server = create_server(
        state_dir=state_dir,
        rpc_url=rpc_url,
        escrow_address=escrow_address,
        token_address=token_address,
        buyer_account=buyer_account,
        max_budget_usd=max_budget_usd,
        max_duration_hours=max_duration_hours,
        allowed_categories=allowed_categories,
        auto_mint=auto_mint,
    )
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        app = server.streamable_http_app(transport_security=sec)
        uvicorn.run(app, host=host, port=port, log_level="info")


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
        "--allowed-categories",
        default=os.environ.get("WARRANT_ALLOWED_CATEGORIES", str(DEFAULT_CATEGORY)),
        help=f"Comma-separated list of allowed category IDs (default: {DEFAULT_CATEGORY})",
    )
    parser.add_argument(
        "--no-auto-mint",
        action="store_true",
        default=os.environ.get("WARRANT_NO_AUTO_MINT", "").lower() in ("1", "true"),
        help="Disable automatic test token minting if balance is insufficient",
    )
    parser.add_argument(
        "--state-dir",
        type=pathlib.Path,
        default=pathlib.Path(os.environ.get("WARRANT_BUYER_STATE_DIR", "artifacts/buyer_state")),
        help="Directory to persist buyer policy state (default: artifacts/buyer_state)",
    )
    args = parser.parse_args()

    cat_list = [int(c.strip()) for c in args.allowed_categories.split(",") if c.strip()]
    transport = "http" if args.transport in ("http", "streamable-http") else "stdio"

    run_server(
        transport=transport,
        host=args.host,
        port=args.port,
        state_dir=args.state_dir,
        rpc_url=args.rpc_url,
        escrow_address=args.escrow_address,
        token_address=args.token_address,
        buyer_account=args.buyer_account,
        max_budget_usd=args.max_budget_usd,
        max_duration_hours=args.max_duration_hours,
        allowed_categories=cat_list,
        auto_mint=not args.no_auto_mint,
    )


if __name__ == "__main__":
    main()
