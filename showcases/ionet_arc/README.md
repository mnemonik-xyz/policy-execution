# io.net GPU Workload Pilot with Warrant on Arc

This showcase demonstrates autonomous cloud compute procurement, container deployment, and
on-chain escrow settlement on Arc (chain ID `5042002`) using the Warrant policy framework.

An autonomous buyer LLM agent interacts with an io.net GPU compute seller MCP server. A local
Buyer Warrant MCP guardrail verifies policy rules (budget cap, lease duration, deterministic
policy hash) before funding `TaskEscrow` on Arc. Once escrow is funded, the seller provisions a
`faster-whisper` transcription worker container on io.net, settles the escrow on-chain, and
transcribes audio submitted by the buyer.

> [!IMPORTANT]
> For the complete dual-MCP protocol and tool specifications, see [README.mcp.md](README.mcp.md).
> For the manual CLI deployment runbook without MCP, see [README.dev.md](README.dev.md).

---

## Showcase Limitations

This showcase is an infrastructure and agent-interaction pilot. It contains several important
architectural simplifications and trust boundaries:

1. **No Real Zero-Knowledge (ZK) Proof**:
   - The on-chain `TaskEscrow` settlement on Arc uses mock / digest journal verification
     (`settle_mock`), passing a 384-byte plain authorization journal.
   - It does **not** generate or verify cryptographic zero-knowledge Groth16 or STARK proofs from
     a RISC Zero zk-prover (such as Bonsai).
2. **No Proof of Deployed Container or Hardware**:
   - The compute seller provides **no cryptographic or verifiable proof** that the container was
     actually deployed on io.net, that the agreed GPU hardware was allocated, or that the container
     is executing honestly.
   - The `deliverable_record` and `deliverable_hash` recorded on-chain are unilaterally asserted by
     the seller, not authenticated by hardware attestations (e.g. Confidential Computing / TEE)
     or independent third parties.
3. **Immediate Escrow Settlement (Upfront Fund Extraction)**:
   - The seller MCP server invokes `TaskEscrow.settle()` **immediately upon container launch** (or
     mock local worker startup) to claim and withdraw the buyer's escrowed USDC.
   - The buyer's funds are extracted **before** the buyer submits their audio workload or receives
     any transcription results.
   - If the container crashes, fails to initialize, or goes unreachable, the buyer has already
     surrendered their payment without automated on-chain dispute resolution or refund recourse.
4. **Trust Boundary**:
   - The Buyer Warrant MCP guardrail successfully enforces local buyer constraints (budget ceiling,
     duration limit, and deterministic policy hash matching) prior to committing funds on-chain.
   - However, once funds are deposited into `TaskEscrow`, the actual execution phase relies on
     counterparty trust in the seller rather than trustless verification.

---

## Architecture and Proof Boundary

- **Buyer LLM Agent**: Interacts with the seller to discover GPUs, request proposals, and submit
  audio jobs.
- **Buyer Warrant MCP Server (`buyer_mcp_server.py`)**: Local trusted guardrail sidecar. Holds
  wallet keys, enforces spending limits, derives/verifies `warrant-policy` hashes, and signs
  `TaskEscrow.offer()`.
- **Seller MCP Server (`seller_mcp_server.py`)**: Remote service hosted by the provider. Discovers
  suitable single-GPU hardware on io.net, generates proposals with dynamic hardware pricing,
  accepts tasks, provisions containers, and settles deliverables on Arc.
- **Arc `TaskEscrow` Contract**: Holds buyer USDC in escrow until valid terms and deliverable
  settlement are executed.
- **Transcription Worker (`worker.py`)**: CUDA/FP16 `faster-whisper` container exposing HTTP
  endpoints (`/health`, `/job`, `/jobs`) with structured operational logs and GPU telemetry.

---

## Arc and USDC Parameters

Official network parameters for Arc:

| Parameter | Arc mainnet | Arc testnet |
| --- | --- | --- |
| Chain ID | 5042 | 5042002 |
| RPC URL | `https://rpc.mainnet.arc.io` | `https://rpc.testnet.arc.io` |
| USDC ERC-20 | `0x3600000000000000000000000000000000000000` | Same address |
| ERC-20 decimals | 6 | 6 |
| Native gas decimals | 18 | 18 |

Native and ERC-20 USDC share a unified balance on Arc. Warrant token amounts use six-decimal
integer units ($1.00 \text{ USDC} = 1\,000\,000$ atomic units), reserving native USDC for gas fees.

---

## Installation and Testing

Python 3.12+ is supported. Install dependencies from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r showcases/ionet_arc/requirements.txt
```

Run unit tests:

```sh
.venv/bin/python -m unittest discover showcases/ionet_arc -v
```

Python sources are linted and formatted with [Ruff](https://docs.astral.sh/ruff/):

```sh
.venv/bin/pip install -r showcases/ionet_arc/requirements-dev.txt
.venv/bin/ruff check showcases/ionet_arc
.venv/bin/ruff format --check showcases/ionet_arc
```

---

## Deploying Contracts and Running the Demonstration

### 1. Build Host and Obtain Guest Image ID

Build the Rust `warrant-host` binary to inspect the policy guest image ID:

```sh
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
IMAGE_ID=$(target/release/warrant-host image-id)
echo "Warrant Guest Image ID: $IMAGE_ID"
```

### 2. Deploy Contracts to Arc Testnet

Deploy `TaskEscrow` and `RiscZeroGroth16Verifier` from the `contracts/` directory using Foundry:

```sh
cd contracts

export WARRANT_RPC_URL=https://rpc.testnet.arc.io
export WARRANT_TOKEN=0x3600000000000000000000000000000000000000
export WARRANT_IMAGE_ID="$IMAGE_ID"
export WARRANT_DEPLOYER="<your-deployer-address>"

forge script script/Deploy.s.sol:Deploy --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --broadcast --slow
```

### 3. Run the Seller MCP Server

Start the Seller MCP daemon configured with your Arc Testnet contract addresses:

```sh
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 0.0.0.0 --port 8000 \
  --rpc-url https://rpc.testnet.arc.io \
  --escrow-address <DEPLOYED_TASK_ESCROW> \
  --token-address 0x3600000000000000000000000000000000000000 \
  --verifier-address <DEPLOYED_VERIFIER> \
  --server-account <SERVER_RECIPIENT_ADDRESS>
```

Add `--mock-ionet` for offline local testing without an io.net API key. When `--rpc-url` targets
local Anvil (`http://127.0.0.1:8545`), escrow contracts are deployed automatically.

### 4. Run the Buyer Warrant MCP Server

Start the Buyer Warrant guardrail sidecar:

```sh
.venv/bin/python -m showcases.ionet_arc.buyer_mcp_server \
  --transport http \
  --host 127.0.0.1 --port 8001 \
  --rpc-url https://rpc.testnet.arc.io \
  --escrow-address <DEPLOYED_TASK_ESCROW> \
  --token-address 0x3600000000000000000000000000000000000000 \
  --buyer-account <BUYER_WALLET_ADDRESS> \
  --max-budget-usd 2.00
```

For desktop LLM agent environments (Claude Desktop / Cursor / Antigravity), specify
`--transport stdio`. See [README.mcp.md](README.mcp.md) for full configuration details.

### 5. Run the Autonomous Client Agent

Launch the client agent with an audio file (e.g. `./sample.mp3`):

```sh
.venv/bin/python -m showcases.ionet_arc.buyer_test_agent \
  --mcp-url http://127.0.0.1:8000/mcp \
  --buyer-mcp-url http://127.0.0.1:8001/mcp \
  --rpc-url https://rpc.testnet.arc.io \
  --audio ./sample.mp3
```

The agent runs the end-to-end lifecycle:
1. Discovers suitable single-GPU hardware on io.net (`seller:list_suitable_hardware`).
2. Requests an agreement proposal with dynamic pricing (`seller:propose_deployment`).
3. Evaluates proposal terms and funds escrow on Arc (`buyer:warrant_evaluate_and_offer`).
4. Provisions container and settles escrow (`seller:deploy_with_escrow`).
5. Transcribes audio on the active Whisper container (`seller:transcribe_audio`).
6. Inspects deployment health and status (`seller:get_deployment_status`).

---

## Worker HTTP API and Operational Logs

The Whisper container HTTP service (`worker.py`) exposes the following endpoints:

| Request | Authentication | Result |
| --- | --- | --- |
| `GET /health` | None | Process health status |
| `POST /job` | `Bearer WORKER_TOKEN` | Upload raw audio, receive job status and `job_id` |
| `GET /job/<job_id>` | Same token | Status, transcript text, segments, and timing for one job |
| `GET /jobs` | Same token | Status of all submitted jobs |

Uploads require `Content-Length` and `X-Audio-SHA256`. An optional language code may be passed
via `?language=<code>` or `X-Language: <code>`. Maximum upload size is 64 MiB. The `job_id` is
the lowercase SHA-256 hash of the input audio bytes.

The worker writes human-readable operational logs with job ID prefixes, elapsed execution time,
and best-effort `nvidia-smi` GPU telemetry (utilization, memory used/total, temperature, power).
Set `WARRANT_LOG_JSON=1` for raw JSON logs.

---

## Manual CLI Deployment Runbook

For running manual container discovery, price estimation, deployment, and teardown via the
standalone CLI (`scripts/ionet-showcase.py`) without MCP agents, refer to the
[Developer Runbook](README.dev.md).

---

## Primary References

- [io.net Agent Cloud documentation](https://io.net/docs/guides/clouds/agent-cloud)
- [CaaS container deployment reference](https://io.net/docs/reference/caas/deploy-a-container)
- [CaaS price estimation reference](https://io.net/docs/reference/caas/price-estimation)
- [Arc connection details and RPC](https://docs.arc.io/arc/references/connect-to-arc)
- [Arc EVM differences](https://docs.arc.io/integrate/evm-differences)
- [Circle USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)
- [faster-whisper repository](https://github.com/SYSTRAN/faster-whisper)
