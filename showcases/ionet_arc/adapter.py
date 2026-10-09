"""Small io.net MCP adapter. No wallet keys, automatic payments, or write retries."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation

ENDPOINT = "https://mcp.io.solutions/mcp"
READ_TOOLS = frozenset({
    "caas_get_hardware_ids", "caas_get_max_gpus_per_container",
    "caas_get_available_replicas", "caas_get_price_estimate",
    "caas_list_deployments", "caas_get_deployment",
    "caas_get_deployment_containers", "get_credit_status",
})
DEPLOY = "caas_deploy_container"
DESTROY = "caas_destroy_deployment"


class PilotError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save(path, value, *, exclusive=False):
    """Persist before a remote mutation. Existing state must never be truncated."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pilot-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(tmp, path)  # atomic no-clobber; prevents two first deployments
        else:
            os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path_or_str):
    if isinstance(path_or_str, (str, Path)):
        p = Path(path_or_str)
        try:
            if p.is_file():
                return json.loads(p.read_text())
        except OSError:
            pass
        s = str(path_or_str).strip()
        if s.startswith(("{", "[")):
            return json.loads(s)
    return json.loads(Path(path_or_str).read_text())


def no_credentials(value):
    """Authentication belongs in the transport header, never request artifacts."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace("-", "_") in {"auth", "api_key", "x_api_key", "private_key", "authorization"}:
                raise PilotError("Pass credentials through the environment, not arguments")
            no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            no_credentials(item)


def decode_result(result):
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        err = texts[0] if texts else "no details"
        print(f"io.net error details: {err}", file=sys.stderr)
        raise PilotError("io.net returned a tool error; no automatic retry was made")
    data = result.get("structuredContent")
    if data is None:
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if len(texts) != 1:
            raise PilotError("Expected one JSON tool result")
        try:
            data = json.loads(texts[0])
        except (ValueError, TypeError):
            raise PilotError("io.net returned non-JSON tool content") from None
    if not isinstance(data, (dict, list)):
        raise PilotError("Unexpected tool result shape")
    if isinstance(data, dict) and (data.get("error") or data.get("status") in {"error", "failed"}):
        raise PilotError("io.net reported failure; inspect the deployment before retrying")
    return data


def check_remote_refs(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "$ref" and not str(v).startswith("#"):
                raise PilotError("Remote schema references are unsupported")
            check_remote_refs(v)
    elif isinstance(value, list):
        for v in value:
            check_remote_refs(v)


def prepare_deploy_call(schema, arguments):
    call_arguments = arguments
    if "request" in schema.get("required", []) and "request" not in arguments:
        req = dict(arguments)
        if isinstance(req.get("location_ids"), (str, int)):
            req["location_ids"] = [req["location_ids"]]
        if isinstance(req.get("hardware_id"), str):
            extra = dict(req.get("extra_payload") or {})
            extra["hardware_id"] = req["hardware_id"]
            if req.get("location_ids") is not None:
                extra["location_ids"] = req["location_ids"]
            req["extra_payload"] = extra
            req["hardware_id"] = 1
            req["location_ids"] = None
        call_arguments = {"request": req}
    return schema, call_arguments


class Cloud:
    def __init__(self, session, schemas):
        self.session, self.schemas = session, schemas

    async def call(self, name, arguments):
        from jsonschema import validators
        no_credentials(arguments)
        if name not in READ_TOOLS | {DEPLOY, DESTROY}:
            raise PilotError("Tool is outside this pilot's allowlist")
        schema = self.schemas.get(name)
        if schema is None:
            raise PilotError("Required tool is missing from io.net discovery")
        check_remote_refs(schema)
        schema_to_validate, call_arguments = (prepare_deploy_call(schema, arguments)
                                              if name == DEPLOY else (schema, arguments))
        validator = validators.validator_for(schema_to_validate)
        validator.check_schema(schema_to_validate)
        if not validator(schema_to_validate).is_valid(call_arguments):
            raise PilotError("Arguments do not match the discovered tool inputSchema")
        result = await self.session.call_tool(name, arguments=call_arguments, read_timeout_seconds=60)
        return decode_result(result.model_dump(by_alias=True, exclude_none=True))


async def connected(operation):
    """Use the pinned official SDK for sessions, SSE and protocol negotiation."""
    key = os.environ.get("IO_NET_API_KEY")
    if not key:
        raise PilotError("Set IO_NET_API_KEY with io-cloud access in the process environment")
    import httpx2
    from mcp import ClientSession, types
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(headers={"x-api-key": key}, timeout=60, follow_redirects=False) as http:
        async with streamable_http_client(ENDPOINT, http_client=http) as streams:
            async with ClientSession(*streams, read_timeout_seconds=60) as session:
                await session.initialize()
                schemas, cursor, seen = {}, None, set()
                for _ in range(100):
                    page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                    for tool in page.tools:
                        if tool.name in READ_TOOLS | {DEPLOY, DESTROY}:
                            schemas[tool.name] = tool.input_schema
                    cursor = page.next_cursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise PilotError("Repeated tools/list cursor")
                    seen.add(cursor)
                else:
                    raise PilotError("Too many tools/list pages")
                return await operation(Cloud(session, schemas))


def validate_deployment(arguments):
    no_credentials(arguments)
    req = arguments.get("request", arguments)
    if req.get("billing_model") not in ("duration", "payg"):
        raise PilotError("Pilot deployments require explicit duration or payg billing")
    for field in ("duration_hours", "gpus_per_container", "replica_count"):
        value = req.get(field)
        if type(value) is not int or value < 1:
            raise PilotError(f"{field} must be a positive integer")
    if req["duration_hours"] > 1 or req["gpus_per_container"] != 1 or req["replica_count"] != 1:
        raise PilotError("First pilot is limited to one GPU, one replica, one hour")
    hw = req.get("hardware_id")
    if type(hw) not in (int, str) or not hw:
        raise PilotError("CaaS requires a valid hardware_id")
    locations = req.get("location_ids")
    loc = locations[0] if isinstance(locations, list) and len(locations) == 1 else locations
    if type(loc) not in (int, str) or not loc or req.get("node_pool_id") is not None:
        raise PilotError("First pilot requires exactly one location_id")


def estimate_cost(estimate, arguments, usd_pointer, max_cost_usd, now=None):
    """Bind the reviewed USD field to a recent estimate for these exact resources.

    The provider does not document a stable price-result schema. The operator
    selects the total USD JSON pointer after inspecting the real response.
    This is a preflight estimate limit, not a provider-enforced billing cap.
    """
    now = time.time() if now is None else now
    if estimate.get("tool") != "caas_get_price_estimate" or not 0 <= now - estimate.get("observed_at", 0) <= 300:
        raise PilotError("Need a price estimate observed within the last five minutes")
    req = arguments.get("request", arguments)
    est_args = estimate.get("arguments", {})
    for key in ("hardware_id", "duration_hours", "gpus_per_container", "replica_count"):
        if est_args.get(key) != req.get(key):
            raise PilotError("Estimate resources do not match the deployment")
    est_loc = est_args.get("location_ids")
    req_loc = req.get("location_ids")
    if est_loc != req_loc and [est_loc] != req_loc and est_loc != [req_loc]:
        raise PilotError("Estimate resources do not match the deployment")
    if not usd_pointer.startswith("/"):
        raise PilotError("Select the total USD field using a JSON pointer starting with /")
    value = estimate.get("result")
    try:
        for token in usd_pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        cost, limit = Decimal(str(value)), Decimal(str(max_cost_usd))
        if not cost.is_finite() or not limit.is_finite() or not 0 < cost <= limit:
            raise ValueError()
    except (TypeError, KeyError, IndexError, ValueError, InvalidOperation):
        raise PilotError("Missing/invalid total USD price, or estimate exceeds the budget") from None
    return str(cost)


def extract_deployment_id(result):
    if not isinstance(result, dict):
        return None
    for candidate in (
        result.get("deployment_id"),
        result.get("id"),
        result.get("cluster_id"),
        result.get("container_deployment_id"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate
    data = result.get("data")
    if isinstance(data, dict):
        return extract_deployment_id(data)
    elif isinstance(data, str) and data:
        return data
    return None


async def deploy(cloud, arguments, state_path, estimate, usd_pointer, max_cost_usd):
    validate_deployment(arguments)
    cost = estimate_cost(estimate, arguments, usd_pointer, max_cost_usd)
    state = {"version": 1, "endpoint": ENDPOINT, "request_sha256": digest(arguments),
             "created_at": int(time.time()), "phase": "submission_unknown",
             "estimate_sha256": digest(estimate), "estimated_cost_usd": cost,
             "max_estimated_cost_usd": str(max_cost_usd)}
    # Persist the intent before any remote call. Reusing this path is always rejected.
    # A timeout may occur AFTER io.net created a billable deployment.
    save(state_path, state, exclusive=True)
    result = await cloud.call(DEPLOY, arguments)
    if isinstance(result, dict) and result.get("status") == "payment_required":
        state.update(phase="payment_required", payment=result.get("payment"))
    else:
        deployment_id = extract_deployment_id(result)
        if not isinstance(deployment_id, str) or not deployment_id:
            raise PilotError("No deployment_id returned; reconcile before another deployment")
        state.update(phase="deployed", deployment_id=deployment_id)
    save(state_path, state)
    return state


async def destroy(cloud, state_path):
    state = read_json(state_path)
    if state.get("endpoint") != ENDPOINT or not state.get("deployment_id"):
        raise PilotError("State has no confirmed io.net deployment_id")
    if state.get("phase") == "destroy_requested":
        raise PilotError("Destroy already requested; check provider status before retrying")
    state["phase"] = "destroy_unknown"
    save(state_path, state)
    result = await cloud.call(DESTROY, {"deployment_id": state["deployment_id"]})
    destroyed_id = extract_deployment_id(result)
    is_ok = (
        destroyed_id == state["deployment_id"]
        or result.get("status") in {"success", "ok"}
        or (isinstance(result.get("data"), dict) and result["data"].get("status") in {"success", "ok"})
    )
    if not isinstance(result, dict) or not is_ok:
        raise PilotError("Destroy response did not identify this deployment")
    # An acknowledgement is not proof that resources/billing have stopped.
    state.update(phase="destroy_requested", destroy_requested_at=int(time.time()))
    save(state_path, state)
    return state
