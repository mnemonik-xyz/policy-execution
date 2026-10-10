#!/usr/bin/env python3
"""Autonomous User Agent client for Warrant io.net transcription showcase.

Demonstrates the 4-step Warrant lifecycle:
1. Query available hardware via list_suitable_hardware.
2. Request a deployment proposal via propose_deployment.
3. Validate proposal against local Warrant policy and fund TaskEscrow on-chain.
4. Call deploy_with_escrow to provision the container and settle the escrow deliverable.
5. Call transcribe_audio on given audio file (adv.mp3) and print the transcript.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from showcases.ionet_arc.escrow import find_binary


def run_cmd(*args, cwd=ROOT):
    cmd = [find_binary(str(args[0])), *[str(a) for a in args[1:]]]
    res = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(str(a) for a in args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def send_tx(rpc_url: str, from_addr: str, *cast_args):
    cmd = [
        find_binary("cast"),
        "send",
        *[str(a) for a in cast_args],
        "--from",
        from_addr,
        "--unlocked",
        "--rpc-url",
        rpc_url,
        "--json",
    ]
    res = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"cast send failed: {res.stderr.strip() or res.stdout.strip()}")
    receipt = json.loads(res.stdout)
    if int(receipt.get("status", "0"), 16) != 1:
        raise RuntimeError(f"Transaction reverted: {receipt}")
    return receipt


def call_view(rpc_url: str, *cast_args):
    cmd = [find_binary("cast"), "call", *[str(a) for a in cast_args], "--rpc-url", rpc_url]
    res = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"cast call failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def extract_result(res):
    if res.content and len(res.content) > 0 and hasattr(res.content[0], "text"):
        try:
            return json.loads(res.content[0].text)
        except Exception:
            pass
    if hasattr(res, "structuredContent") and res.structuredContent:
        sc = res.structuredContent
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        return sc
    return {}


async def run_client(
    mcp_url: str,
    rpc_url: str,
    audio_path: Path,
    auth_user: str | None = None,
    auth_pass: str | None = None,
    max_budget_usdc: float = 1.00,
):
    print("=" * 70)
    print("  WARRANT AUTONOMOUS TRANSCRIPTION AGENT")
    print("=" * 70)

    # 1. Inspect on-chain environment
    accounts = json.loads(run_cmd("cast", "rpc", "eth_accounts", "--rpc-url", rpc_url))
    customer = accounts[0]
    agent = accounts[1] if len(accounts) > 1 else accounts[0]
    print(f"Customer wallet: {customer}")
    print(f"Agent wallet:    {agent}")
    print(f"Audio file:      {audio_path} ({audio_path.stat().st_size} bytes)")

    headers = {}
    if auth_user and auth_pass:
        import base64
        creds = base64.b64encode(f"{auth_user}:{auth_pass}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {creds}"

    async with httpx2.AsyncClient(headers=headers, timeout=60) as http_client:
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
                for hw in (hw_list[:5] if isinstance(hw_list, list) else []):
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
                print("Proposal received from server:")
                print(f"  • Task ID:        {proposal['task_id']}")
                print(f"  • Hardware:       {proposal['hardware_name']} (${proposal['price_per_hour_usd']:.4f}/hr)")
                print(f"  • Duration:       {proposal['duration_hours']} hour")
                print(f"  • Escrow amount:  {proposal['amount']} atomic units (${proposal['amount_usd']} USDC)")
                print(f"  • Policy hash:    {proposal['policy_hash']}")
                print(f"  • TaskEscrow:     {proposal['escrow_address']}")

                # Step 3: Policy Verification & Escrow Payment
                print("\n[Step 3] Verifying proposal against local Warrant policy...")
                proposed_cost = float(proposal["amount_usd"])
                if proposed_cost > max_budget_usdc:
                    raise RuntimeError(
                        f"Policy rejection: proposed cost ${proposed_cost} exceeds budget cap ${max_budget_usdc}"
                    )
                if proposal["duration_hours"] != 1:
                    raise RuntimeError("Policy rejection: duration must be exactly 1 hour for pilot")
                print(" Policy evaluation: APPROVED (within budget and category constraints)")

                escrow_addr = proposal["escrow_address"]
                token_addr = proposal["token_address"]
                amount = int(proposal["amount"])
                salt = proposal["salt"]
                recipient = proposal["recipient"]
                policy_hash = proposal["policy_hash"]
                policy_version = int(proposal["policy_version"])
                now = int(time.time())
                accept_by = now + 3600
                settle_by = now + 86400

                # Ensure customer has enough token balance & allowance
                balance_str = call_view(rpc_url, token_addr, "balanceOf(address)(uint256)", customer).split()[0]
                if int(balance_str) < amount:
                    print(f"Minting test tokens to {customer}...")
                    send_tx(rpc_url, customer, token_addr, "mint(address,uint256)", customer, amount * 10)

                print("Approving TaskEscrow allowance...")
                send_tx(rpc_url, customer, token_addr, "approve(address,uint256)", escrow_addr, amount)

                print("Submitting TaskEscrow.offer() on-chain...")
                offer_tx = send_tx(
                    rpc_url,
                    customer,
                    escrow_addr,
                    "offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)",
                    salt,
                    recipient,
                    policy_hash,
                    policy_version,
                    amount,
                    accept_by,
                    settle_by,
                )
                print(f" TaskEscrow funded on-chain: tx {offer_tx['transactionHash']}")

                # Step 4: deploy_with_escrow
                print("\n[Step 4] Calling deploy_with_escrow on MCP server...")
                res_dep = await session.call_tool("deploy_with_escrow", {"task_id": proposal["task_id"]})
                dep_info = extract_result(res_dep)
                print("Deployment provisioned and escrow settled:")
                print(f"  • Deployment ID: {dep_info.get('deployment_id')}")
                print(f"  • Public URL:    {dep_info.get('public_url')}")
                print(f"  • Settlement TX: {dep_info.get('settlement_transaction')}")
                print("  • Duration:      1 hour (auto-destroys upon completion)")

                # Step 5: transcribe_audio
                print(f"\n[Step 5] Transcribing audio file '{audio_path.name}' via MCP...")
                start_t = time.time()
                res_trans = await session.call_tool(
                    "transcribe_audio",
                    {"audio_path": str(audio_path.resolve()), "language": "en"},
                )
                trans_result = extract_result(res_trans)
                elapsed = time.time() - start_t

                print("\n" + "=" * 70)
                print("  TRANSCRIPTION RESULT")
                print("=" * 70)
                print(f"Status:        {trans_result.get('status')}")
                print(f"Language:      {trans_result.get('language')}")
                print(f"Audio length:  {trans_result.get('audio_seconds')}s")
                print(f"Latency:       {elapsed:.2f}s")
                print(f"Text:\n\n\"{trans_result.get('text')}\"\n")
                if trans_result.get("segments"):
                    print("Segments:")
                    for seg in trans_result["segments"]:
                        print(f"  [{seg['start']:.2f}s - {seg['end']:.2f}s] {seg['text'].strip()}")
                print("=" * 70)

                # Step 6: get_deployment_status
                print("\n[Step 6] Inspecting live deployment session status...")
                res_stat = await session.call_tool("get_deployment_status", {})
                stat = extract_result(res_stat)
                print(f"Active session:    {stat.get('active')}")
                print(f"Remaining time:    {stat.get('remaining_seconds')} seconds in 1h window")
                print(f"Settlement tx:     {stat.get('settlement_transaction')}")
                print("\n Autonomous Warrant transcription showcase completed successfully!")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default="http://localhost:8000/mcp", help="MCP server streamable HTTP endpoint")
    parser.add_argument("--rpc-url", default="http://127.0.0.1:8545", help="Ethereum / Arc JSON-RPC URL")
    parser.add_argument(
        "--audio",
        type=Path,
        default=ROOT / "adv.mp3",
        help="Audio file to transcribe (default: adv.mp3)",
    )
    parser.add_argument("--auth-user", default=os.environ.get("MCP_AUTH_USER"), help="HTTP Basic Auth user")
    parser.add_argument("--auth-pass", default=os.environ.get("MCP_AUTH_PASS"), help="HTTP Basic Auth pass")
    parser.add_argument("--max-budget", type=float, default=1.00, help="Max budget ceiling in USDC (default: 1.00)")
    args = parser.parse_args()

    if not args.audio.exists():
        sys.exit(f"Error: audio file '{args.audio}' does not exist.")

    asyncio.run(
        run_client(
            mcp_url=args.mcp_url,
            rpc_url=args.rpc_url,
            audio_path=args.audio,
            auth_user=args.auth_user,
            auth_pass=args.auth_pass,
            max_budget_usdc=args.max_budget,
        )
    )


if __name__ == "__main__":
    main()
