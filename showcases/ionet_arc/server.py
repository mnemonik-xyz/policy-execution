"""Streamable HTTP MCP server for external agents driving io.net GPU compute."""
import base64
import json
import pathlib
import secrets
import sys
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
        name="ionet_discover",
        description="Inspect the discovered inputSchemas of all allowed tools on io.net.",
    )
    async def ionet_discover() -> dict:
        async def op(cloud):
            return {"inputSchemas": cloud.schemas}

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_read",
        description=(
            "Execute an allowlisted read query on io.net.\n\n"
            "Supported tools and their required argument shapes:\n"
            "1. Zero-argument tools (pass `{}` for arguments):\n"
            "   - get_credit_status: `{}`\n"
            "   - caas_get_hardware_ids: `{}`\n"
            "   - caas_get_max_gpus_per_container: `{}`\n"
            "   - caas_list_deployments: `{}` (optional: page, page_size, status)\n\n"
            "2. Deployment inspection tools:\n"
            "   - caas_get_deployment: `{\"deployment_id\": \"<uuid>\"}`\n"
            "   - caas_get_deployment_containers: `{\"deployment_id\": \"<uuid>\"}`\n\n"
            "3. Pricing estimate (all 5 fields are required!):\n"
            "   - caas_get_price_estimate: {\n"
            "       \"hardware_id\": \"gpu_1x_l40\",\n"
            "       \"location_ids\": \"US\",\n"
            "       \"duration_hours\": 1,\n"
            "       \"gpus_per_container\": 1,\n"
            "       \"replica_count\": 1\n"
            "     }\n\n"
            "Note: Use string hardware IDs (e.g. 'gpu_1x_l40'). To check replica availability, "
            "inspect 'available_replica_count' in the caas_get_price_estimate response."
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
        name="ionet_check_credits",
        description="Check current io.net account credits and balance status.",
    )
    async def ionet_check_credits() -> dict:
        async def op(cloud):
            res = await cloud.call("get_credit_status", {})
            return {
                "tool": "get_credit_status",
                "observed_at": int(time.time()),
                "result": res,
            }

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_list_hardware",
        description="List available GPU hardware types, specifications, and cluster options on io.net.",
    )
    async def ionet_list_hardware(node_pool_id: str = None) -> dict:
        arguments = {"node_pool_id": node_pool_id} if node_pool_id else {}

        async def op(cloud):
            res = await cloud.call("caas_get_hardware_ids", arguments)
            return {
                "tool": "caas_get_hardware_ids",
                "observed_at": int(time.time()),
                "result": res,
            }

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_get_pricing",
        description=(
            "Get price estimate and replica availability for a GPU type.\n"
            "Parameters:\n"
            "- hardware_id: Regional GPU string ID (e.g. 'gpu_1x_l40', 'gpu_4x_h100')\n"
            "- location_id: Region code string (default 'US', or 'FR', 'PL', etc.)\n"
            "- duration_hours: Duration in hours (default 1, max 1 for pilot)"
        ),
    )
    async def ionet_get_pricing(
        hardware_id: str,
        location_id: str = "US",
        duration_hours: int = 1,
    ) -> dict:
        arguments = {
            "hardware_id": hardware_id,
            "location_ids": location_id,
            "duration_hours": duration_hours,
            "gpus_per_container": 1,
            "replica_count": 1,
        }

        async def op(cloud):
            res = await cloud.call("caas_get_price_estimate", arguments)
            return {
                "tool": "caas_get_price_estimate",
                "arguments": arguments,
                "observed_at": int(time.time()),
                "result": res,
            }

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_get_deployment_status",
        description="Inspect an active deployment's health, ingress endpoint, public URL, and container events.",
    )
    async def ionet_get_deployment_status(deployment_id: str) -> dict:
        if not deployment_id:
            raise ToolError("deployment_id is required")

        async def op(cloud):
            containers = await cloud.call(
                "caas_get_deployment_containers", {"deployment_id": deployment_id}
            )
            deployment = await cloud.call(
                "caas_get_deployment", {"deployment_id": deployment_id}
            )
            return {
                "deployment_id": deployment_id,
                "observed_at": int(time.time()),
                "deployment": deployment,
                "containers": containers,
            }

        try:
            return await connected(op)
        except Exception as exc:
            raise safe_tool_error(exc) from None

    @server.tool(
        name="ionet_list_deployments",
        description="List your CaaS deployments on io.net.",
    )
    async def ionet_list_deployments(
        page: int = 1, page_size: int = 10, status: str = None
    ) -> dict:
        arguments = {"page": page, "page_size": page_size}
        if status:
            arguments["status"] = status

        async def op(cloud):
            res = await cloud.call("caas_list_deployments", arguments)
            return {
                "tool": "caas_list_deployments",
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
            "Safely deploy a CaaS container on io.net under budget and replica limits.\n"
            "Automatically queries pricing, checks budget against max_cost_usd, journals write intent "
            "to disk, launches the container, and returns the state record.\n\n"
            "Parameters:\n"
            "- max_cost_usd: Budget limit ceiling (e.g. '1.00')\n"
            "- hardware_id: Regional GPU string ID (default 'gpu_1x_l40')\n"
            "- location_id: Region code string (default 'US')\n"
            "- image_url: Container registry image URL\n"
            "- resource_name: Deployment name (default 'warrant-worker')\n"
            "- traffic_port: Port to expose (default 8080)\n"
            "- env_variables: Optional dict of container environment variables\n"
            "- deploy_args: Optional full raw deployment dictionary"
        ),
    )
    async def ionet_deploy(
        max_cost_usd: str,
        hardware_id: str = "gpu_1x_l40",
        location_id: str = "US",
        image_url: str = "",
        resource_name: str = "warrant-worker",
        traffic_port: int = 8080,
        env_variables: dict = None,
        billing_model: str = "duration",
        usd_pointer: str = "",
        deploy_args: dict = None,
    ) -> dict:
        try:
            if deploy_args is None:
                deploy_args = {
                    "billing_model": billing_model,
                    "hardware_id": hardware_id,
                    "location_ids": location_id,
                    "duration_hours": 1,
                    "gpus_per_container": 1,
                    "replica_count": 1,
                    "resource_private_name": resource_name,
                    "image_url": image_url,
                    "traffic_port": traffic_port,
                    "env_variables": env_variables or {},
                }
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
    host: str = "localhost",
    port: int = 8000,
    state_dir: pathlib.Path = pathlib.Path("artifacts/state"),
    auth_user: str | None = None,
    auth_pass: str | None = None,
):
    """Run the MCP server via uvicorn."""
    app = create_app(state_dir, auth_user, auth_pass)
    uvicorn.run(app, host=host, port=port, log_level="info")
