# io.net MCP Server for External Agents

This standalone daemon exposes io.net GPU compute capabilities over Streamable HTTP (`/mcp`)
using the Python MCP SDK (`mcp.server.mcpserver`). It provides guarded lifecycle tools with
budget validation, persistent write-intent journaling, and optional HTTP Basic Authentication.

## Running the server

Start the daemon from the repository root:

```sh
.venv/bin/python scripts/ionet-mcp-server.py \
  --host 0.0.0.0 --port 8000 \
  --auth-user <USER> --auth-pass <PASS>
```

Configurable options (and environment variables):
- `--host` / `HOST`: Bind address (default: `0.0.0.0`).
- `--port` / `PORT`: Port number (default: `8000`).
- `--state-dir` / `IONET_STATE_DIR`: State journal directory (default: `artifacts/state`).
- `--auth-user` / `MCP_AUTH_USER`: HTTP Basic Auth username (disabled if unset).
- `--auth-pass` / `MCP_AUTH_PASS`: HTTP Basic Auth password (disabled if unset).

Ensure `IO_NET_API_KEY` is present in the server's environment so it can authenticate with
upstream io.net.

## Exposed tools

Primary typed tools:
- `ionet_check_credits`: Check account balance status.
- `ionet_list_hardware`: List available GPU hardware and specifications.
- `ionet_get_pricing`: Get price estimate and availability for a GPU and region.
- `ionet_deploy`: Budget-checked, atomically journaled container deployment.
- `ionet_get_deployment_status`: Inspect deployment health, ingress URL, and logs.
- `ionet_list_deployments`: List CaaS deployments.
- `ionet_destroy`: Guarded termination of an active deployment by `deployment_id`.

Escape-hatch tools:
- `ionet_discover`: Inspect raw JSON inputSchemas.
- `ionet_read`: Execute raw allowlisted read tools (`caas_get_hardware_ids`, etc.).

## Testing locally with curl

### 1. Verify basic auth rejection

Unauthenticated requests to `/mcp` receive a `401 Unauthorized` challenge:

```sh
curl -i http://localhost:8000/mcp
```

Expected response:
```http
HTTP/1.1 401 Unauthorized
www-authenticate: Basic realm="MCP"
content-length: 12

Unauthorized
```

### 2. Initialize MCP session handshake

Under the MCP Streamable HTTP specification, clients first call `initialize`. The server
returns `200 OK` and an `mcp-session-id` header:

```sh
curl -i -u admin:secret \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "curl-test", "version": "1.0"}
    }
  }' \
  http://localhost:8000/mcp
```

Look for the `mcp-session-id` header in the response:
```http
HTTP/1.1 200 OK
content-type: text/event-stream
mcp-session-id: <SESSION_ID>
...
```

### 3. List available tools

Pass the extracted session ID in the `mcp-session-id` header:

```sh
SESSION_ID="<your-session-id>"

curl -i -u admin:secret \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list"
  }' \
  http://localhost:8000/mcp
```

### 4. Execute a read query (tools/call)

Query read-only status without modifying resources:

```sh
curl -i -u admin:secret \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "ionet_read",
      "arguments": {
        "tool": "get_credit_status",
        "arguments": {}
      }
    }
  }' \
  http://localhost:8000/mcp
```

### Quick automated test script

Capture the session ID and list tools in a single script:

```sh
INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize",'
INIT_BODY+='"params":{"protocolVersion":"2024-11-05","capabilities":{},'
INIT_BODY+='"clientInfo":{"name":"test","version":"1"}}}'

SESSION_ID=$(curl -s -i -u admin:secret \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d "$INIT_BODY" \
  http://localhost:8000/mcp | grep -i '^mcp-session-id:' | awk '{print $2}' | tr -d '\r')

curl -s -u admin:secret \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  http://localhost:8000/mcp
```
