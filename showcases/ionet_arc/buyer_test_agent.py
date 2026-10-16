#!/usr/bin/env python3
"""Autonomous User Agent client for Warrant io.net container deployment showcase.

Demonstrates the capability-based two-step escrow lifecycle:
1. Query available hardware via list_suitable_hardware.
2. Request a deployment proposal via propose_deployment.
3. Validate proposal against local Warrant policy and fund TaskEscrow on-chain.
4. Call deploy_with_escrow to accept the task and begin background provisioning.
5. Poll get_deployment_status until the worker container is ready.
6. Verify container health endpoint (/health) with worker bearer token.
7. Sign Acceptance evidence and call submit_acceptance to settle TaskEscrow on-chain.
8. Inspect final deployment status via get_deployment_status.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx2  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from showcases.ionet_arc.buyer_mcp_server import BuyerWarrantService  # noqa: E402
from showcases.ionet_arc.escrow import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    EscrowClient,
    EthRpc,
    get_manifest_parameter,
    load_manifest,
    should_trust_env,
)


def extract_result(res):
    is_err = getattr(res, "is_error", False) or getattr(res, "isError", False)
    if is_err:
        err_msg = res.content[0].text if (res.content and hasattr(res.content[0], "text")) else "MCP ToolError"
        return {"status": "ERROR", "error": err_msg}
    if res.content and len(res.content) > 0 and hasattr(res.content[0], "text"):
        try:
            return json.loads(res.content[0].text)
        except Exception:
            return {"status": "ERROR", "error": res.content[0].text}
    if hasattr(res, "structuredContent") and res.structuredContent:
        sc = res.structuredContent
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        return sc
    return {}


async def get_policy_via_buyer_mcp(buyer_mcp_url: str) -> dict:
    async with httpx2.AsyncClient(timeout=60, trust_env=should_trust_env(buyer_mcp_url)) as http_client:
        async with streamable_http_client(buyer_mcp_url, http_client=http_client) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=60) as session:
                await session.initialize()
                res = await session.call_tool("warrant_get_policy", {})
                return extract_result(res)


async def fund_via_buyer_mcp(buyer_mcp_url: str, proposal: dict) -> dict:
    async with httpx2.AsyncClient(timeout=60, trust_env=should_trust_env(buyer_mcp_url)) as http_client:
        async with streamable_http_client(buyer_mcp_url, http_client=http_client) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=60) as session:
                await session.initialize()
                res = await session.call_tool("warrant_evaluate_and_offer", {"proposal": proposal})
                return extract_result(res)


async def sign_acceptance_via_buyer_mcp(
    buyer_mcp_url: str,
    task_id: str,
    deliverable_hash: str,
    recipient: str,
    amount: int,
) -> dict:
    async with httpx2.AsyncClient(timeout=60, trust_env=should_trust_env(buyer_mcp_url)) as http_client:
        async with streamable_http_client(buyer_mcp_url, http_client=http_client) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=60) as session:
                await session.initialize()
                res = await session.call_tool(
                    "warrant_sign_acceptance",
                    {
                        "task_id": task_id,
                        "deliverable_hash": deliverable_hash,
                        "recipient": recipient,
                        "amount": amount,
                    },
                )
                return extract_result(res)


async def poll_deployment_ready(session: ClientSession, task_id: str, max_polls: int = 60) -> dict:
    """Poll get_deployment_status until container status is ready or fails."""
    for poll_i in range(max_polls):
        res_stat = await session.call_tool("get_deployment_status", {"task_id": task_id})
        stat = extract_result(res_stat)
        st = stat.get("status")
        if st == "ready":
            return stat
        if st == "failed":
            sys.exit(f"Deployment failed: {stat.get('error')}")
        print(f"  • Polling ({poll_i + 1}/{max_polls}): status={st} ...")
        await asyncio.sleep(2)
    sys.exit("Timeout waiting for deployment to become ready.")


def _resolve_agent_wallets(rpc_url: str, manifest_path: Path | None = None) -> tuple[str, str | None]:
    m_path = manifest_path or (
        Path(os.environ["WARRANT_MANIFEST"]) if "WARRANT_MANIFEST" in os.environ else DEFAULT_MANIFEST_PATH
    )
    manifest = load_manifest(m_path)
    customer = get_manifest_parameter(manifest, "buyer_address")
    agent = get_manifest_parameter(manifest, "seller_address")

    if not customer or not agent:
        try:
            accounts = EthRpc(rpc_url).accounts()
        except Exception:
            accounts = []
        if not customer and accounts:
            customer = accounts[0]
        if not agent and accounts:
            agent = accounts[1] if len(accounts) > 1 else accounts[0]

    if not customer:
        sys.exit("Error: Customer wallet not configured in manifest.yaml and no unlocked accounts returned by RPC.")

    return customer, agent


async def _resolve_buyer_account(
    rpc_url: str, manifest_path: Path | None, buyer_mcp_url: str | None
) -> tuple[str, str | None]:
    customer, agent = _resolve_agent_wallets(rpc_url, manifest_path)
    if buyer_mcp_url:
        policy_info = await get_policy_via_buyer_mcp(buyer_mcp_url)
        if policy_info.get("buyer_account"):
            customer = policy_info["buyer_account"]
    return customer, agent


async def _evaluate_and_fund_proposal(
    buyer_mcp_url: str | None,
    proposal: dict,
    rpc_url: str,
    customer: str,
    max_budget_usdc: float,
    manifest_path: Path | None,
) -> tuple[dict, BuyerWarrantService | None]:
    if buyer_mcp_url:
        print(f"\n[Step 3] Delegating proposal evaluation to Buyer Warrant MCP ({buyer_mcp_url})...")
        offer_res = await fund_via_buyer_mcp(buyer_mcp_url, proposal)
        return offer_res, None

    print("\n[Step 3] Verifying proposal against local Warrant policy...")
    service = BuyerWarrantService(
        state_dir=ROOT / "artifacts" / "buyer_state",
        escrow_client=EscrowClient(rpc_url=rpc_url, server_account=customer),
        max_budget_usd=max_budget_usdc,
        buyer_account=customer,
        auto_mint=True,
        manifest_path=manifest_path,
    )
    offer_res = await service.warrant_evaluate_and_offer(proposal)
    return offer_res, service


async def _sign_acceptance(
    buyer_mcp_url: str | None,
    service: BuyerWarrantService | None,
    proposal: dict,
    deliverable_hash: str,
) -> dict:
    if buyer_mcp_url:
        return await sign_acceptance_via_buyer_mcp(
            buyer_mcp_url=buyer_mcp_url,
            task_id=proposal["task_id"],
            deliverable_hash=deliverable_hash,
            recipient=proposal["recipient"],
            amount=proposal["amount"],
        )
    return await service.warrant_sign_acceptance(
        task_id=proposal["task_id"],
        deliverable_hash=deliverable_hash,
        recipient=proposal["recipient"],
        amount=proposal["amount"],
    )


async def run_client(
    mcp_url: str,
    rpc_url: str,
    auth_user: str | None = None,
    auth_pass: str | None = None,
    max_budget_usdc: float | None = None,
    buyer_mcp_url: str | None = None,
    manifest_path: Path | None = None,
):
    if max_budget_usdc is None:
        m = load_manifest(manifest_path or DEFAULT_MANIFEST_PATH)
        raw_budget = get_manifest_parameter(m, "max_budget_usd")
        if raw_budget is None:
            sys.exit(f"max_budget_usd is not configured in {manifest_path or DEFAULT_MANIFEST_PATH}")
        max_budget_usdc = float(raw_budget)

    print("=" * 70)
    print("  WARRANT AUTONOMOUS BUYER AGENT")
    print("=" * 70)

    # 1. Inspect on-chain environment
    customer, agent = await _resolve_buyer_account(rpc_url, manifest_path, buyer_mcp_url)
    print(f"Customer wallet: {customer}")
    print(f"Agent wallet:    {agent}")

    headers = {}
    if auth_user and auth_pass:
        creds = base64.b64encode(f"{auth_user}:{auth_pass}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {creds}"

    async with httpx2.AsyncClient(headers=headers, timeout=60, trust_env=should_trust_env(mcp_url)) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, read_timeout_seconds=60) as session:
                await session.initialize()
                print("\n Connected to Warrant MCP server over Streamable HTTP")

                # Step 1: list_suitable_hardware
                print("\n[Step 1] Querying available suitable GPU hardware...")
                res_hw = await session.call_tool("list_suitable_hardware", {})
                hw_list = extract_result(res_hw)
                if isinstance(hw_list, dict) and "result" in hw_list:
                    hw_list = hw_list["result"]
                print(f"Discovered {len(hw_list)} suitable single-GPU candidates on io.net:")
                for hw in hw_list[:5] if isinstance(hw_list, list) else []:
                    name = hw["hardware_name"]
                    hw_id = hw["hardware_id"]
                    price = hw["price_per_hour_usd"]
                    loc = hw["location"]
                    reps = hw["available_replicas"]
                    print(f"  • {name} ({hw_id}): ${price:.4f}/hr [{loc}] — {reps} replicas available")

                # Step 2: propose_deployment
                print("\n[Step 2] Requesting deployment proposal from server...")
                res_prop = await session.call_tool(
                    "propose_deployment",
                    {"customer_address": customer, "budget_cap_usd": str(max_budget_usdc)},
                )
                proposal = extract_result(res_prop)
                if proposal.get("status") == "ERROR" or "task_id" not in proposal:
                    sys.exit(f"Failed to propose deployment: {proposal.get('error', proposal)}")
                print("Proposal received from server:")
                print(f"  • Task ID:        {proposal['task_id']}")
                print(f"  • Hardware:       {proposal['hardware_name']} (${proposal['price_per_hour_usd']:.4f}/hr)")
                print(f"  • Duration:       {proposal['duration_hours']} hour")
                print(f"  • Escrow amount:  {proposal['amount']} atomic units (${proposal['amount_usd']} USDC)")
                print(f"  • TaskEscrow:     {proposal['escrow_address']}")

                # Step 3: Policy Verification & Escrow Payment
                offer_res, service = await _evaluate_and_fund_proposal(
                    buyer_mcp_url=buyer_mcp_url,
                    proposal=proposal,
                    rpc_url=rpc_url,
                    customer=customer,
                    max_budget_usdc=max_budget_usdc,
                    manifest_path=manifest_path,
                )

                if offer_res.get("status") != "APPROVED":
                    sys.exit(f"Policy evaluation or escrow funding failed: {offer_res}")

                print(f" Policy evaluation: {offer_res.get('status', 'APPROVED')}")
                print(f" Policy hash:       {offer_res.get('policy_hash')}")
                print(f" TaskEscrow funded on-chain: tx {offer_res.get('transaction_hash')}")

                # Step 4: deploy_with_escrow
                print("\n[Step 4] Calling deploy_with_escrow on MCP server...")
                res_dep = await session.call_tool(
                    "deploy_with_escrow",
                    {"task_id": proposal["task_id"], "policy": offer_res.get("policy")},
                )
                dep_info = extract_result(res_dep)
                if dep_info.get("status") == "ERROR":
                    sys.exit(f"deploy_with_escrow failed: {dep_info.get('error')}")
                print(f"Deployment initiated: status={dep_info.get('status')}")

                # Step 5: Poll get_deployment_status until container is ready
                print("\n[Step 5] Polling deployment status until worker container is ready...")
                ready_stat = await poll_deployment_ready(session, proposal["task_id"])

                public_url = ready_stat["public_url"].rstrip("/")
                worker_token = ready_stat.get("worker_token")
                deliverable_hash = ready_stat.get("deliverable_hash")
                print("Worker container is online:")
                print(f"  • Public URL:       {public_url}")
                print(f"  • Deliverable hash: {deliverable_hash}")

                # Step 6: Health Verification
                health_url = f"{public_url}/health"
                print(f"\n[Step 6] Verifying container health endpoint ({health_url})...")
                trust_env = should_trust_env(public_url)
                async with httpx2.AsyncClient(timeout=10.0, trust_env=trust_env) as client:
                    resp = await client.get(health_url, headers={"Authorization": f"Bearer {worker_token}"})
                    if resp.status_code != 200:
                        sys.exit(f"Worker health check failed with HTTP {resp.status_code}: {resp.text}")
                    print(f" Container healthy: {resp.text.strip()}")

                # Step 7: Deliverable sign-off & Escrow settlement
                print("\n[Step 7] Signing Acceptance and submitting evidence for escrow settlement...")
                sign_res = await _sign_acceptance(
                    buyer_mcp_url=buyer_mcp_url,
                    service=service,
                    proposal=proposal,
                    deliverable_hash=deliverable_hash,
                )
                print(f" Acceptance signed: status={sign_res.get('status')}")

                res_settle = await session.call_tool(
                    "submit_acceptance",
                    {
                        "task_id": proposal["task_id"],
                        "acceptance": sign_res["acceptance"],
                        "signature": sign_res["signature"],
                    },
                )
                settle_info = extract_result(res_settle)
                if settle_info.get("status") == "ERROR":
                    sys.exit(f"submit_acceptance failed: {settle_info.get('error')}")
                print(f" TaskEscrow settled on-chain: tx {settle_info.get('settlement_transaction')}")

                # Step 8: Final inspection of live deployment session status
                print("\n[Step 8] Inspecting final deployment session status...")
                res_stat = await session.call_tool("get_deployment_status", {"task_id": proposal["task_id"]})
                stat = extract_result(res_stat)
                print(f"Active session:    {stat.get('active')}")
                print(f"Status:            {stat.get('status')}")
                print(f"Public URL:        {stat.get('public_url')}")
                print(f"Remaining time:    {stat.get('remaining_seconds')} seconds in 1h window")
                print(f"Settlement tx:     {stat.get('settlement_transaction')}")
                print("\n Autonomous Warrant container deployment completed successfully!")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default="http://localhost:8000/mcp", help="MCP server streamable HTTP endpoint")
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("WARRANT_RPC_URL", os.environ.get("ARC_RPC_URL")),
        help="Ethereum / Arc JSON-RPC URL (default: read from manifest.yaml or http://127.0.0.1:8545)",
    )
    parser.add_argument("--auth-user", default=os.environ.get("MCP_AUTH_USER"), help="HTTP Basic Auth user")
    parser.add_argument("--auth-pass", default=os.environ.get("MCP_AUTH_PASS"), help="HTTP Basic Auth pass")
    parser.add_argument(
        "--max-budget",
        type=float,
        default=None,
        help="Max budget ceiling in USDC (default: from manifest.yaml)",
    )
    parser.add_argument(
        "--buyer-mcp-url",
        default=os.environ.get("BUYER_MCP_URL"),
        help="Optional Buyer Warrant MCP server streamable HTTP endpoint",
    )
    parser.add_argument(
        "--manifest",
        dest="manifest_path",
        type=Path,
        default=os.environ.get("WARRANT_MANIFEST"),
        help="Optional path to manifest.yaml config",
    )
    args = parser.parse_args()

    rpc_url = args.rpc_url
    if not rpc_url:
        manifest = load_manifest(args.manifest_path or DEFAULT_MANIFEST_PATH)
        rpc_url = get_manifest_parameter(manifest, "rpc_url") or "http://127.0.0.1:8545"

    asyncio.run(
        run_client(
            mcp_url=args.mcp_url,
            rpc_url=rpc_url,
            auth_user=args.auth_user,
            auth_pass=args.auth_pass,
            max_budget_usdc=args.max_budget,
            buyer_mcp_url=args.buyer_mcp_url,
            manifest_path=args.manifest_path,
        )
    )


if __name__ == "__main__":
    main()
