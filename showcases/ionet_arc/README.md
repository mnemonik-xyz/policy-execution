# io.net workload pilot with Warrant on Arc

This is the first infrastructure milestone for a real solver-bounty showcase:
a buyer purchases a verified schedule, a separate runner executes a useful
transcription workload on io.net, and Warrant settles the solver fee on Arc.

Implemented here: an MCP client, a bounded CaaS deployment flow, persistent
write-intent records, a GPU transcription HTTP worker, read-only Arc checks,
and validation of provider payment requests. **Automatic bridging, payment,
and settlement-triggered workload execution are not implemented yet.** These
files do not claim live GPU execution or public-chain settlement. The existing
[solver demo](../../solver-bounty/README.md) remains the local proof/payment demo.

## Responsibilities and proof boundary

- **Buyer and seller**: Agree to jobs, resources, runtime estimates, prices and fees.
  *Evidence*: Buyer-approved committed inputs.
- **Warrant on Arc**: Pay for a schedule satisfying the committed constraints.
  *Evidence*: Pinned solver guest and Groth16 verification.
- **io.net adapter**: Discover, estimate, provision, inspect, terminate.
  *Evidence*: Authenticated provider API.
- **Transcription worker**: Run audio job, return transcript and timing.
  *Evidence*: Cloud execution evidence; no correctness proof.
- **CCTP service (next milestone)**: Fund a Solana operational wallet from Arc.
  *Evidence*: Circle attestation and destination confirmation.
- **Provider payment service (next milestone)**: Pay the current exact top-up request once.
  *Evidence*: Solana transaction and provider credit confirmation.

The bounty and infrastructure budgets are separate. Cross-chain funding stays
outside the escrow and is not atomic with Warrant settlement. The schedule
proof does not prove cloud execution, truthful prices, actual runtimes,
transcription quality, optimality, or realized savings.

The v1 checker sums job-duration × units × price. A duration-billed deployment
can include idle time and startup overhead. Keep this distinction visible
until a versioned billing-aware checker is introduced. A whole-deployment
quote must not be substituted for `price_per_unit_tick` and presented as a
proven cloud bill.

## Arc and USDC funding

Official documentation checked on 2026-10-04:

| Parameter | Arc mainnet | Arc testnet |
| --- | --- | --- |
| Chain ID | 5042 | 5042002 |
| RPC | `https://rpc.mainnet.arc.io` | `https://rpc.testnet.arc.io` |
| USDC ERC-20 | `0x3600000000000000000000000000000000000000` | Same address |
| ERC-20 decimals | 6 | 6 |
| Native gas decimals | 18 | 18 |
| CCTP domain | 26 | 26 |

Native and ERC-20 USDC share one balance. Use six-decimal integer units for
Warrant token amounts and reserve native USDC for gas. Do not double-count the
two views. Chain IDs are not CCTP domain IDs.

Circle lists Arc and Solana (domain 5) as CCTP-supported networks. App Kit's
Bridge capability / standalone Bridge Kit manages burn, attestation and mint.
The intended operational flow is:

1. Keep the bounty and settlement on Arc.
2. Quote a CCTP transfer to our operational wallet on Solana, accounting for
   fees so the net received amount covers the provider payment.
3. Journal burn transaction, message, attestation and mint progress. Resume an
   incomplete transfer rather than burning again.
4. Confirm destination USDC balance and arrange Solana transaction fees or a
   configured sponsor.
5. Revalidate the current io.net request's network, native USDC mint, recipient
   and exact amount; pay it once and record the transaction.
6. Confirm account credit and reconcile before retrying deployment.

Direct CCTP minting to the provider's address is not assumed to satisfy its
payment matching. Bridge fees must not reduce the provider payment. Testnet
USDC cannot purchase real compute; the parser rejects a testnet funding route
for a mainnet payment request.

`payment-plan` produces a **non-executable** description. Its bridge gross
amount remains unset until a fee quote exists. It never signs or marks a bill
paid. Automatic bridging requires the next milestone's wallet integration,
transaction journal, reconciliation and spending policy.

## Install and inspect

Python 3.12 is used in CI. Run from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r showcases/ionet_arc/requirements.txt
.venv/bin/python -m unittest showcases.ionet_arc.test_pilot -v
.venv/bin/python scripts/ionet-showcase.py arc-check \
  --network testnet --out artifacts/arc-preflight.json
```

`arc-check --network mainnet` is also read-only. It checks RPC chain ID and
USDC ERC-20 decimals; it does not validate the verifier or deploy contracts.
Local mocked RPC tests are not public-network validation. Arc-specific EVM
behavior needs Arc testnet or Arc Foundry testing before deployment.

Configure `IO_NET_API_KEY` in the process environment with `io-cloud` access.
Do not put it in argument files. The client sends it to the documented MCP
endpoint using the `x-api-key` header. The official MCP SDK handles sessions
and transport.

```sh
.venv/bin/python scripts/ionet-showcase.py discover \
  --out artifacts/io-tools.json
.venv/bin/python scripts/ionet-showcase.py read caas_get_hardware_ids \
  --out artifacts/io-hardware.json
.venv/bin/python scripts/ionet-showcase.py read get_credit_status \
  --out artifacts/io-credit-before.json
```

Form arguments from the discovered `inputSchemas`. Calls validate against
those schemas and fail on mismatches. The REST deployment docs currently
specify the body as `any`; this pilot does not invent a complete request.
Captured responses have restrictive local file permissions but may contain
account data or container environment variables. Publish reviewed evidence,
not raw account responses.

For running the standalone Streamable HTTP MCP server for external agents,
see [README.mcp.md](README.mcp.md).

## Deploying contracts and running on Arc Testnet

The Warrant transcription showcase connects io.net container provisioning with on-chain
`TaskEscrow` verification and settlement on Arc Testnet (chain ID `5042002`).

### 1. Build host and obtain guest image ID

From the repository root, build the Rust `warrant-host` binary to inspect the policy guest image ID:

```sh
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
IMAGE_ID=$(target/release/warrant-host image-id)
echo "Warrant Guest Image ID: $IMAGE_ID"
```

### 2. Deploy contracts to Arc Testnet

Deploy `TaskEscrow` and `RiscZeroGroth16Verifier` using Foundry from the `contracts/` directory:

```sh
cd contracts

export WARRANT_RPC_URL=https://rpc.testnet.arc.io
export WARRANT_TOKEN=0x3600000000000000000000000000000000000000
export WARRANT_IMAGE_ID="$IMAGE_ID"
export WARRANT_DEPLOYER="<your-deployer-address>"

# Simulation run:
forge script script/Deploy.s.sol:Deploy --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --slow

# Broadcast transactions on Arc Testnet:
forge script script/Deploy.s.sol:Deploy --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --broadcast --slow
```

Record the deployed contract addresses from Foundry output:
- `TaskEscrow` contract address
- `RiscZeroGroth16Verifier` contract address
- Arc Testnet USDC address: `0x3600000000000000000000000000000000000000`

### 3. Run the Warrant transcription MCP server

Start the MCP daemon configured with your Arc Testnet addresses and `IO_NET_API_KEY`:

```sh
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 0.0.0.0 --port 8000 \
  --rpc-url https://rpc.testnet.arc.io \
  --escrow-address <DEPLOYED_TASK_ESCROW> \
  --token-address 0x3600000000000000000000000000000000000000 \
  --verifier-address <DEPLOYED_VERIFIER> \
  --server-account <SERVER_RECIPIENT_ADDRESS>
```

For offline dry runs without io.net API keys, add `--mock-ionet`. If `--rpc-url` points to
a local Anvil instance (`http://127.0.0.1:8545`), the server automatically deploys local test
escrow contracts.

> [!NOTE]
> Settlement in the local MCP demo uses mock journal verification (`settle_mock`). For production
> settlement on Arc Testnet with `RiscZeroGroth16Verifier`, a cryptographic Groth16 seal must
> be generated via the RISC0 prover (e.g., Bonsai) matching the guest image ID.

### 4. Run the autonomous client agent

In a separate terminal, launch the autonomous client agent with an audio file (e.g. `./adv.mp3`):

```sh
.venv/bin/python -m showcases.ionet_arc.buyer_test_agent \
  --mcp-url http://127.0.0.1:8000/mcp \
  --rpc-url https://rpc.testnet.arc.io \
  --audio ./sample.mp3
```

The client agent performs the end-to-end flow:
1. Discovers suitable Whisper single GPUs on io.net (`list_suitable_hardware`).
2. Requests an agreement proposal (`propose_deployment`).
3. Verifies proposal against local Warrant policy constraints.
4. Funds `TaskEscrow.offer()` on Arc Testnet using testnet USDC.
5. Invokes `deploy_with_escrow`: server accepts on-chain, provisions the container on io.net
   for 1-hour duration, and executes `TaskEscrow.settle()` with a 384-byte journal.
6. Calls `transcribe_audio` through MCP to transcribe `./adv.mp3` and print output + segments.
7. Inspects deployment health and settlement status via `get_deployment_status`.

## First live deployment (legacy CLI)

The pilot allows exactly one GPU, one replica, one hour, and explicit `billing_model: "duration"`
or `"payg"`. Note that PayG does not bound container lifetime on the provider side: destroy
promptly after testing to prevent ongoing metered billing.

1. Select an integer CaaS hardware ID and one integer location ID from live
   discovery and availability. Regional string hardware IDs are unsuitable
   for the documented CaaS deployment tool.
2. Build and publish the worker image to your registry. Use the resulting
   immutable image digest when deploying. Image publication is an operator
   step; this adapter does not guess a registry or publish images.

   ```sh
   docker build -t warrant-transcription:pilot showcases/ionet_arc
   ```

3. Generate a random worker token of at least 32 characters and pass it through
   the container environment as `WARRANT_WORKER_TOKEN`. This is distinct from
   the io.net API key. Expose HTTP port 8080. The image downloads a pinned model
   revision at build time.
4. Create `price-args.json` with `hardware_id`, `location_ids`, `duration_hours`,
   `gpus_per_container`, and `replica_count`, following discovered schemas.
   Create `deploy-args.json` with matching resources, explicit duration billing,
   and the image/port/environment fields required by the deployment schema.
5. Fetch the estimate and inspect its total USD field and included charges:

   ```sh
   .venv/bin/python scripts/ionet-showcase.py read caas_get_price_estimate \
     --args price-args.json --out artifacts/io-estimate.json
   ```

6. Deploy using the total USD field's JSON pointer and an agreed limit:

   ```sh
   .venv/bin/python scripts/ionet-showcase.py deploy \
     --args deploy-args.json --estimate artifacts/io-estimate.json \
     --usd-pointer /REPLACE_WITH_TOTAL_USD_FIELD \
     --max-cost-usd AGREED_LIMIT --state artifacts/io-deployment.json
   ```

The pointer is relative to the provider result. The adapter rejects estimates
older than five minutes, changed resources, invalid prices and estimates over
the limit. This is an estimate gate, not an atomic provider billing cap. Bound
available cloud credits for the pilot and measure actual charges separately.

A `payment_required` response is persisted and stops the flow. To inspect an
Arc-funded route using a fresh state:

```sh
.venv/bin/python scripts/ionet-showcase.py payment-plan \
  --state artifacts/io-deployment.json --network mainnet \
  --solana-wallet YOUR_OPERATIONAL_PUBLIC_KEY \
  --max-payment-usdc AGREED_LIMIT --out artifacts/io-payment-plan.json
```

This does not pay. An already funded io.net account can support the first
infrastructure spike; label that as prefunding, not automated bridging.

## Execute and collect

Use `read caas_get_deployment` and `read caas_get_deployment_containers` with
arguments from discovery to find the assigned public HTTPS endpoint.

| Request | Authentication | Result |
| --- | --- | --- |
| `GET /health` | None | Process health; no GPU-readiness claim |
| `POST /job` | `Bearer WORKER_TOKEN` | Upload raw audio, receive job status & `job_id` |
| `GET /job/<job_id>` | Same token | Status, transcript, hashes, revision and timing for one job |
| `GET /jobs` | Same token | All submitted jobs |

For uploads, supply `Content-Length` and `X-Audio-SHA256` (lowercase SHA-256 of the audio). An
optional language code can be passed via `?language=<code>` or `X-Language: <code>`. Maximum
size is 64 MiB. No caller-supplied URL is fetched. The response `job_id` is the input SHA-256;
use it to poll `GET /job/<job_id>`. Repeated submission of the same input returns its existing
job, while different inputs create independent jobs. `GET /job` remains available only when
there is zero or one job, for compatibility. Collect output outside the container before cleanup.
Jobs transcribe one at a time to avoid GPU contention. A restart with preserved state reports
unfinished work as `interrupted`.

The worker uses CUDA/FP16 `faster-whisper`. Timing includes model loading and
transcription. Hashes identify bytes, not correctness. Local tests use an
explicit fake transcription engine and do not claim a GPU/model run.

## Operational logs

The worker writes structured JSON events to its standard output: `worker_started`,
`job_accepted`, `job_started`, `job_completed`, `job_failed`, and restart interruption events.
Job events contain only the job ID, byte count, queue depth, elapsed time, and requested
language—not authorization values, audio bytes, or transcript text. Startup and job start/end
events include a best-effort `nvidia-smi` snapshot for each GPU: utilization, memory used/total,
temperature, and power draw. Long-running jobs emit `job_running` snapshots every 10 seconds;
set `WARRANT_GPU_LOG_INTERVAL_SECONDS` (minimum: 1) to adjust that cadence. On CPU deployments
or when telemetry is unavailable, the event contains an `available: false` reason and
transcription continues normally. Set `WARRANT_LOG_LEVEL` to adjust the standard `INFO` logging
level.

## Cleanup and recovery

```sh
.venv/bin/python scripts/ionet-showcase.py destroy \
  --state artifacts/io-deployment.json
```

Only the saved deployment ID is targeted. A destroy acknowledgement is recorded
as `destroy_requested`, not proof of stopped resources/billing. Read deployment
status and credits afterwards and retain the observations.

| State | Required action |
| --- | --- |
| `submission_unknown` | Inspect deployments: the request may have succeeded before timeout. |
| `payment_required` | Resolve credits/payment, then reconcile before another attempt. |
| `deployed` | Collect results and request cleanup. |
| `destroy_unknown` | Inspect that deployment before retrying cleanup. |
| `destroy_requested` | Confirm termination and billing cessation with the provider. |

The intent is fsynced before a deployment call. Reusing its state filename is
rejected. Paid calls are never automatically retried. Keep original evidence;
use a new state file only after reconciling whether another attempt is needed.
This is not a claim of provider-side idempotency.

## Remaining milestones

1. Authenticated io.net spike: capture real response schemas, run the built image
   on a GPU, collect a useful transcript, measure charges, confirm termination.
   Requires API access, image registry, public audio and an infrastructure budget.
2. Add CCTP/Bridge Kit signing/recovery and exact provider payment. Rehearse Arc
   testnet → Solana devnet separately from real io.net billing.
3. Benchmark a useful batch and version the billing-aware checker. Compare a
   simple baseline, seller schedules and total economic cost.
4. Connect independent buyer/seller/relayer/runner processes. Bind Arc chain,
   escrow, token and image in the policy; prevent duplicate provisioning when
   replaying settlement events.
5. Validate solver-specific proofs and settlement on Arc testnet. Existing
   `scripts/check-arc.py` covers a read-only task/invoice constructor probe,
   not solver settlement or funding integration.
6. Review contracts and operations, run a bounded Arc mainnet pilot, publish
   evidence, then update Warrant/Tameion pins to the reviewed implementation.

Publish input/model/image commitments, schedules, proof and settlement,
cloud lifecycle records, transcript output, funding transactions where used,
measured cloud/prover/gas costs and cleanup evidence. Claim savings only when
those measurements support them.

## Primary references

- [io.net Agent Cloud: tools, billing and x402](https://io.net/docs/guides/clouds/agent-cloud)
- [CaaS HTTP container constraints](https://io.net/docs/reference/caas/deploy-a-container)
- [CaaS price parameters](https://io.net/docs/reference/caas/price-estimation)
- [Arc connection details](https://docs.arc.io/arc/references/connect-to-arc)
- [Arc EVM differences](https://docs.arc.io/integrate/evm-differences)
- [Arc contract addresses](https://docs.arc.io/arc/references/contract-addresses)
- [CCTP supported chains and domains][cctp-chains]
- [Circle-issued USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)
- [App Kit Bridge / Bridge Kit](https://docs.arc.io/app-kit/bridge)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

[cctp-chains]: https://developers.circle.com/cctp/concepts/supported-chains-and-domains
