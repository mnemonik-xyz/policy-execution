"""Streamable HTTP MCP server for external agents driving io.net GPU compute."""
import base64
import json
import pathlib
import secrets
import time
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
import uvicorn

from showcases.ionet_arc.adapter import (
    PilotError,
    READ_TOOLS,
    connected,
    deploy,
    destroy,
    read_json,
    save,
    validate_deployment,
)


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
    """Extract known safe pilot exceptions, masking raw transport exceptions."""
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
        "Operation failed. If a write was submitted, inspect saved state and io.net before retrying."
    )


def extract_price_args(deploy_args: dict) -> dict:
    req = deploy_args.get("request", deploy_args)
    price_args = {
        "hardware_id": req.get("hardware_id"),
        "duration_hours": req.get("duration_hours", 1),
        "gpus_per_container": req.get("gpus_per_container", 1),
        "replica_count": req.get("replica_count", 1),
    }
    if "location_ids" in req:
        price_args["location_ids"] = req["location_ids"]
    return price_args


def find_usd_pointer(estimate_obj: dict, candidate_pointer: str = "") -> str:
    if candidate_pointer and candidate_pointer.startswith("/"):
        return candidate_pointer
    val = estimate_obj.get("result", {})
    if isinstance(val, dict):
        if "data" in val and isinstance(val["data"], dict) and "data" in val["data"]:
            if "total_cost_usdc" in val["data"]["data"]:
                return "/data/data/total_cost_usdc"
        if "test_total_usd" in val:
            return "/test_total_usd"
    return candidate_pointer or "/data/data/total_cost_usdc"


def create_server(state_dir: pathlib.Path) -> MCPServer:
    """Create and configure the MCP server instance with guarded compute tools."""
    server = MCPServer("ionet-arc-showcase")
    state_dir = pathlib.Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    @server.tool(
        name="ionet_read",
        description=(
            "Execute an allowlisted read query on io.net. "
            "Available tools: caas_get_hardware_ids, caas_get_max_gpus_per_container, "
            "caas_get_available_replicas, caas_get_price_estimate, caas_list_deployments, "
            "caas_get_deployment, caas_get_deployment_containers, get_credit_status."
        ),
    )
    async def ionet_read(tool: str, arguments: dict = None) -> dict:
        arguments = arguments or {}
        if tool not in READ_TOOLS:
            raise ToolError(
                f"Tool '{tool}' is outside this pilot's allowlist: {sorted(READ_TOOLS)}"
            )

        async def op(cloud):
            res = await cloud.call(tool, arguments)
            return {
                "tool": tool,
                "arguments": arguments,
                "observed_at": int(time.time()),
                "result": res,
            }

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_deploy",
        description=(
            "Safely deploy a CaaS container on io.net under budget and replica limits. "
            "The server automatically queries pricing, validates cost against max_cost_usd, "
            "records the write intent to disk, launches the container, and returns the state."
        ),
    )
    async def ionet_deploy(
        deploy_args: dict,
        max_cost_usd: str,
        usd_pointer: str = "",
    ) -> dict:
        try:
            validate_deployment(deploy_args)
            price_args = extract_price_args(deploy_args)
            run_id = f"run-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            state_path = state_dir / f"{run_id}.json"

            async def op(cloud):
                est_res = await cloud.call("caas_get_price_estimate", price_args)
                estimate_obj = {
                    "tool": "caas_get_price_estimate",
                    "observed_at": int(time.time()),
                    "arguments": price_args,
                    "result": est_res,
                }
                pointer = find_usd_pointer(estimate_obj, usd_pointer)
                state = await deploy(
                    cloud,
                    deploy_args,
                    state_path,
                    estimate_obj,
                    pointer,
                    max_cost_usd,
                )
                dep_id = state.get("deployment_id")
                if dep_id:
                    dep_path = state_dir / f"{dep_id}.json"
                    if not dep_path.exists():
                        save(dep_path, state)
                return state

            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_destroy",
        description=(
            "Safely terminate an active deployment on io.net and update its journal record."
        ),
    )
    async def ionet_destroy(deployment_id: str) -> dict:
        if not deployment_id:
            raise ToolError("deployment_id is required")

        target_path = state_dir / f"{deployment_id}.json"
        if not target_path.exists():
            for candidate in state_dir.glob("*.json"):
                try:
                    st = read_json(candidate)
                    if st.get("deployment_id") == deployment_id:
                        target_path = candidate
                        break
                except Exception:
                    pass
            else:
                raise ToolError(
                    f"No local state record found for deployment_id '{deployment_id}'"
                )

        async def op(cloud):
            state = await destroy(cloud, target_path)
            dep_path = state_dir / f"{deployment_id}.json"
            if dep_path != target_path:
                save(dep_path, state)
            return state

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    return server


def create_app(
    state_dir: pathlib.Path,
    auth_user: str | None = None,
    auth_pass: str | None = None,
):
    """Build the ASGI Starlette app with MCP streamable HTTP transport and optional basic auth."""
    server = create_server(state_dir)
    sec = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(transport_security=sec)

    if auth_user and auth_pass:
        print(f"HTTP Basic Auth enabled for user '{auth_user}'", file=sys.stderr)
        app = BasicAuthMiddleware(app, auth_user, auth_pass)
    else:
        print("WARNING: HTTP Basic Auth disabled (no --auth-user / --auth-pass provided)", file=sys.stderr)

    return app


def run_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    state_dir: pathlib.Path = pathlib.Path("artifacts/state"),
    auth_user: str | None = None,
    auth_pass: str | None = None,
):
    """Run the MCP server via uvicorn."""
    app = create_app(state_dir, auth_user, auth_pass)
    uvicorn.run(app, host=host, port=port, log_level="info")
