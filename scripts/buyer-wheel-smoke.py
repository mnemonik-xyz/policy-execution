"""Start an installed warrant-buyer guardrail over stdio and call one tool.

    python scripts/buyer-wheel-smoke.py <home> <warrant-buyer executable>

<home> is the HOME that `warrant-buyer init` ran with. The call fails when the guardrail does not start,
does not list its four tools, or cannot compute a policy hash with the bundled binaries.
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {
    "warrant_get_policy",
    "warrant_evaluate_and_offer",
    "warrant_sign_acceptance",
    "warrant_sign_request",
    "warrant_check_escrow_status",
}


async def main(home: str, exe: str) -> None:
    base = f"{home}/.warrant-buyer"
    params = StdioServerParameters(
        command=exe,
        args=[
            "guardrail",
            "--transport",
            "stdio",
            "--manifest",
            f"{base}/manifest.yaml",
            "--state-dir",
            f"{base}/state",
        ],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names >= TOOLS, f"missing tools: {TOOLS - names}"
        policy = json.loads((await session.call_tool("warrant_get_policy", {})).content[0].text)
        assert policy["chain_id"] == 5042 and policy["max_budget_usd"] == 1.0, policy
    print("guardrail ok:", sorted(names))


asyncio.run(main(sys.argv[1], sys.argv[2]))
