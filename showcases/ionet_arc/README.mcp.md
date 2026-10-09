# Warrant Autonomous Audio Transcription MCP Showcase

This standalone daemon provides an autonomous MCP service for deploying and consuming private
speech-to-text inference containers on io.net CaaS using the Warrant trust and payment machinery.

Instead of arbitrary cloud management, the server provides a strictly guarded 4-step lifecycle:
1. **Hardware Discovery**: Queries available single-GPU instances on io.net suitable for Whisper.
2. **Proposal Generation**: Evaluates prices and creates a binding task proposal with policy hash.
3. **Escrow-Guarded Deployment**: Verifies on-chain escrow funding via `TaskEscrow.offer()`, accepts
   the task, deploys `ghcr.io/mnemonik-xyz/warrant-transcription:pilot` for a 1-hour fixed duration,
   settles the escrow deliverable on-chain via `TaskEscrow.settle()`, and activates the worker.
4. **Autonomous Transcription**: Transcribes audio files via the active container through MCP.

Deployment uses `billing_model: "duration"` for exactly 1 hour and automatically destroys on
io.net upon completion. No manual destruction tool is required; while active, `transcribe_audio`
can be called repeatedly.

## Running the MCP Server

Start the daemon from the repository root:

```sh
.venv/bin/python scripts/ionet-mcp-server.py \
  --host 0.0.0.0 --port 8000 \
  --rpc-url http://127.0.0.1:8545 \
  --auth-user <USER> --auth-pass <PASS>
```

For local testing without io.net API credentials, pass `--mock-ionet`:

```sh
.venv/bin/python scripts/ionet-mcp-server.py \
  --host 127.0.0.1 --port 8000 \
  --rpc-url http://127.0.0.1:8545 \
  --mock-ionet
```

### Configurable CLI Flags

- `--host`: Bind address (default: `0.0.0.0`, env: `HOST`).
- `--port`: Port number (default: `8000`, env: `PORT`).
- `--state-dir`: Session state and proposal storage (default: `artifacts/state`).
- `--rpc-url`: EVM JSON-RPC endpoint for TaskEscrow (default: `http://127.0.0.1:8545`).
- `--escrow-address`: Deployed `TaskEscrow` contract address (auto-deployed on local Anvil).
- `--token-address`: ERC-20 payment token address (e.g. USDC).
- `--verifier-address`: zkVM/Journal verifier contract address.
- `--server-account`: Server's recipient address for escrow settlement.
- `--mock-ionet`: Run with in-process mock transcription worker instead of live io.net API.
- `--auth-user` / `--auth-pass`: HTTP Basic Auth credentials (optional).

## Exposed MCP Tools

The server exposes 5 focused tools:

1. **`list_suitable_hardware`**
   - Discovers available single-GPU options on io.net suitable for Whisper inference.
   - Filters out multi-GPU clusters and unavailable nodes, sorting by price ascending.

2. **`propose_deployment`**
   - Agent selects the cheapest suitable GPU and computes the 1-hour cost.
   - Generates unique `task_id`, `salt`, and hashes the Warrant policy (`accepted-contractor-v1`).
   - Returns a structured proposal for customer agent verification and on-chain funding.

3. **`deploy_with_escrow`**
   - Arguments: `task_id`.
   - Prerequisites: `propose_deployment` executed and customer called `TaskEscrow.offer()`.
   - Verifies on-chain deposit, accepts task, launches container for 1-hour duration.
   - Settles payment deliverable on `TaskEscrow` with a 384-byte journal and activates worker.

4. **`transcribe_audio`**
   - Arguments: `audio_path`, `language` (optional, e.g. `"en"`).
   - Prerequisite: Active, unexpired 1-hour deployment from `deploy_with_escrow`.
   - Directly uploads audio bytes to the worker and returns full transcription and segments.

5. **`get_deployment_status`**
   - Inspects active container health, public URL, remaining seconds in the 1-hour window,
     and on-chain escrow settlement transaction hash.

## Running the Client Agent Demonstration

The client agent (`scripts/warrant-transcription-client.py`) automates the full client workflow:
1. Connects to the server over MCP Streamable HTTP.
2. Discovers suitable GPUs via `list_suitable_hardware`.
3. Requests a formal proposal via `propose_deployment`.
4. Evaluates the proposal against local Warrant policy (budget cap, category, duration).
5. Funds `TaskEscrow.offer()` on-chain.
6. Calls `deploy_with_escrow` to trigger deployment and verify escrow settlement.
7. Submits audio for transcription via `transcribe_audio` and outputs text + segments.
8. Inspects live session status via `get_deployment_status`.

### Example Run

```sh
.venv/bin/python scripts/warrant-transcription-client.py \
  --mcp-url http://127.0.0.1:8000/mcp \
  --rpc-url http://127.0.0.1:8545 \
  --audio ./adv.mp3
```

## Running Automated Tests

Run the unit and end-to-end integration test suites:

```sh
# Warrant transcription MCP server unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_warrant_transcription -v

# Core io.net CaaS pilot & worker unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_pilot -v

# End-to-end integration test (spins up Anvil, MCP Server, and Client Agent)
.venv/bin/python -m unittest showcases.ionet_arc.test_demo_e2e -v
```
