# Warrant Autonomous Audio Transcription MCP Architecture

This showcase demonstrates an autonomous dual-MCP architecture for cloud compute procurement,
execution, and on-chain escrow settlement on Arc using the Warrant policy framework.

## The Dual-MCP Architecture: Seller Service & Buyer Guardrail

In an autonomous agent economy, buyers interact with untrusted seller agents. An autonomous
buyer LLM agent cannot be entrusted with unrestricted private keys or unconstrained spending:
hallucinations or prompt injections could drain funds or accept malicious contracts.

To solve this, the architecture separates the seller's service from the buyer's local guardrail:

```
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                         Buyer LLM Agent Environment                         │
  │                                                                             │
  │     1. list_suitable_hardware()           3. warrant_evaluate_and_offer()   │
  │     2. propose_deployment()                      (Local Policy Check &      │
  │     4. deploy_with_escrow()                       On-Chain Escrow Funding)  │
  │     5. transcribe_audio()                                │                  │
  └───────────────┬──────────────────────────────────────────┼──────────────────┘
                  │                                          │
                  ▼                                          ▼
   ┌──────────────────────────────┐          ┌──────────────────────────────┐
   │      Seller MCP Server       │          │   Buyer Warrant MCP Server   │
   │  (Remote Streamable HTTP)    │          │    (Local Guardrail Sidecar) │
   │                              │          │                              │
   │ • Discovers io.net GPUs      │          │ • Enforces max budget ($2)   │
   │ • Builds pricing proposals   │          │ • Enforces max duration (1h) │
   │ • Accepts on-chain escrow    │          │ • Verifies policy hash       │
   │ • Provisions Whisper worker  │          │ • Approves & calls offer()   │
   │ • Settles 384-byte journal   │          │ • Tracks escrow state        │
   └──────────────┬───────────────┘          └──────────────┬───────────────┘
                  │                                         │
                  │              Arc Blockchain             │
                  └────────► [ TaskEscrow Contract ] ◄──────┘
```

1. **Seller MCP Server (`seller_mcp_server.py`)**:
   - Hosted by the compute seller (e.g., io.net CaaS provider).
   - Generates concrete task terms, provisions 1-hour Whisper GPU containers, and settles
     deliverables on `TaskEscrow` with a 384-byte verification journal.
   - Exposed over Streamable HTTP (default port 8000).

2. **Buyer Warrant MCP Server (`buyer_mcp_server.py`)**:
   - Runs locally alongside the buyer agent as a trusted guardrail sidecar.
   - Holds the buyer's wallet credentials, spending caps, and category whitelist.
   - Independently computes and verifies the cryptographic `warrant-policy` hash before signing
     any transaction.
   - Approves token allowance and submits `TaskEscrow.offer()` on-chain.
   - Supports `stdio` (for Claude Desktop / Cursor / Antigravity) and Streamable HTTP (port 8001).

---

## Exposed MCP Tools

### Buyer Warrant Guardrail Tools (`buyer_mcp_server.py`)

- **`warrant_get_policy`**:
  Inspect local policy constraints: budget cap, max duration, allowed categories, buyer address,
  and pinned escrow/token contracts.

- **`warrant_evaluate_and_offer`**:
  Arguments: `proposal` (or unpacked task terms: `task_id`, `amount`, `amount_usd`, `salt`,
  `recipient`, `policy_hash`, `duration_hours`, etc.).
  Validates that cost $\le$ max budget, duration $\le$ max duration, category is permitted,
  and independently derives/verifies the `warrant-policy` hash. Approves ERC-20 token allowance
  and submits `TaskEscrow.offer()` on-chain.

- **`warrant_check_escrow_status`**:
  Arguments: `task_id`.
  Queries on-chain `TaskEscrow` state (`Missing`, `Offered`, `Accepted`, `Paid`, `Refunded`).

### Seller Tools (`seller_mcp_server.py`)

- **`list_suitable_hardware`**:
  Discovers available single-GPU options on io.net suitable for Whisper inference, sorted by
  price ascending.

- **`propose_deployment`**:
  Arguments: `budget_cap_usd`, `customer_address`, optional `hardware_id`, optional
  `duration_hours`. Calculates duration cost from real hardware hourly rates, validates budget
  ceiling, generates unique `task_id`, salt, policy hash, and returns formal escrow terms.

- **`deploy_with_escrow`**:
  Arguments: `task_id`.
  Verifies on-chain deposit via `TaskEscrow`, accepts task, provisions the 1-hour container,
  settles the escrow deliverable on-chain via `TaskEscrow.settle()`, and activates the worker.

- **`transcribe_audio`**:
  Arguments: `audio_path`, optional `language`.
  Transcribes audio on the active 1-hour worker and returns text plus timestamped segments.

- **`get_deployment_status`**:
  Inspects worker health, public URL, remaining duration in 1h window, and settlement tx hash.

---

## Desktop Agent Configuration (Claude Desktop, Cursor, Antigravity)

Configure both MCP servers in your agent settings (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "buyer-warrant-guardrail": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": [
        "-m", "showcases.ionet_arc.buyer_mcp_server",
        "--transport", "stdio",
        "--rpc-url", "https://rpc.testnet.arc.io",
        "--max-budget-usd", "2.00",
        "--max-duration-hours", "1"
      ]
    },
    "seller-transcription-service": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": [
        "-m", "showcases.ionet_arc.seller_mcp_server",
        "--rpc-url", "https://rpc.testnet.arc.io"
      ]
    }
  }
}
```

The agent prompts can then naturally discover GPU hardware, request a quote, submit it to
the local Warrant tool for policy check and escrow funding, deploy, and transcribe audio.

---

## Running the Servers Standalone

### 1. Run the Seller MCP Server

```sh
# Live io.net deployment:
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 127.0.0.1 --port 8000 \
  --rpc-url http://127.0.0.1:8545

# Offline testing with mock io.net worker:
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 127.0.0.1 --port 8000 \
  --rpc-url http://127.0.0.1:8545 \
  --mock-ionet
```

### 2. Run the Buyer Warrant MCP Server

```sh
# Run over stdio (standard for desktop LLM agents):
.venv/bin/python -m showcases.ionet_arc.buyer_mcp_server \
  --transport stdio \
  --rpc-url http://127.0.0.1:8545 \
  --max-budget-usd 2.00

# Run over Streamable HTTP (e.g. port 8001):
.venv/bin/python -m showcases.ionet_arc.buyer_mcp_server \
  --transport http \
  --host 127.0.0.1 --port 8001 \
  --rpc-url http://127.0.0.1:8545 \
  --max-budget-usd 2.00
```

### 3. Run the Autonomous Client Agent Demonstration

```sh
# End-to-end client with local Buyer Warrant MCP server:
.venv/bin/python -m showcases.ionet_arc.buyer_test_agent \
  --mcp-url http://127.0.0.1:8000/mcp \
  --buyer-mcp-url http://127.0.0.1:8001/mcp \
  --rpc-url http://127.0.0.1:8545 \
  --audio ./sample.mp3
```

---

## Running Automated Tests

```sh
# Buyer Warrant guardrail MCP server unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_buyer_mcp_server -v

# Seller transcription MCP server unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_warrant_transcription -v

# TaskEscrow Web3 client unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_escrow -v

# Whisper worker unit tests (queue, model caching, telemetry)
.venv/bin/python -m unittest showcases.ionet_arc.test_worker -v

# Core io.net CaaS pilot unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_pilot -v

# End-to-end integration test (spins up Anvil, Seller MCP, Buyer MCP, Agent)
.venv/bin/python -m unittest showcases.ionet_arc.test_demo_e2e -v
```
