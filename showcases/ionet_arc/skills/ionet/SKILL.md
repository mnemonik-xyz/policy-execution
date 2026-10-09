---
name: ionet
description: Guide and validated call formats for driving the io.net GPU compute MCP server.
---

# io.net CaaS Call-Format Skill

Guidelines and verified call formats for driving the io.net tools exposed by this repo's MCP
server (`scripts/ionet-mcp-server.py`).

## Primary typed tools

- `ionet_check_credits`: Check credit balance (no arguments needed).
- `ionet_list_hardware`: List available GPU hardware and specifications.
- `ionet_get_pricing`: Get price estimate and availability:
  `{"hardware_id": "gpu_1x_l40", "location_id": "US", "duration_hours": 1}`.
- `ionet_deploy`: Deploy a container under budget limits:
  `{"max_cost_usd": "1.00", "hardware_id": "gpu_1x_l40", "image_url": "...", "traffic_port": 8080}`.
- `ionet_get_deployment_status`: Inspect container ingress and logs:
  `{"deployment_id": "<uuid>"}`.
- `ionet_list_deployments`: List CaaS deployments (`{"page": 1, "page_size": 10}`).
- `ionet_destroy`: Terminate container (`{"deployment_id": "<uuid>"}`).

---

## Hardware ID classes

- **Regional (string) IDs** (e.g. `"gpu_1x_l40"`, `"gpu_4x_h100"`):
  - `location_id`: Region string (`"US"`, `"FR"`, `"PL"`, etc.)
  - Price estimate works reliably.
  - Recommended for all deployment workflows.
- **Numeric IDs** (e.g. `224`, `12`):
  - Numeric IDs are currently read-only; provider rejects price estimates for numeric IDs.

---

## Recommended deployment workflow

1. Check balance: `ionet_check_credits()`.
2. Find hardware: `ionet_list_hardware()`.
3. Check pricing: `ionet_get_pricing(hardware_id="gpu_1x_l40", location_id="US")`.
4. Deploy container:
   `ionet_deploy(max_cost_usd="1.00", hardware_id="gpu_1x_l40", image_url="...")`.
5. Check status: `ionet_get_deployment_status(deployment_id="<uuid>")`.
6. Terminate: `ionet_destroy(deployment_id="<uuid>")`.

---

## Escape-hatch generic tools

- `ionet_discover`: Inspect raw JSON schemas.
- `ionet_read`: `{"tool": "<tool_name>", "arguments": {<args>}}`.
  Zero-argument tools require `{}`. `caas_get_price_estimate` requires all 5 fields.
