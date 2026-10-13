# Plan: Arc mainnet canary + io.net showcase

## Revision of 2026-10-12 and Phase 0 status of 2026-10-13

The revised plan (2026-10-12) replaces the solver-based plan below where the two differ.
The canary is one real GPU-rental order of at most 2 USDC through `TaskEscrow` on Arc
mainnet with a real proof, driven by the two MCP servers in `showcases/ionet_arc`, then a
Circle Agent Marketplace listing for the seller. `SolverBountyEscrow` and
`scripts/solver-canary.py` stay available but are not the canary path.

### Phase 0: code gaps closed in this repository

| Gap | Status | Where |
|---|---|---|
| Real-proof settlement in the seller server | Done. `settlement: proof` runs `warrant-host prove`, `wrap`, `export-evm` in the background, checks the proof image ID against `TaskEscrow.imageId()`, then calls `settle(seal, journal)`. Mock settlement is refused on chain 5042. | `seller_mcp_server.py`, `escrow.py` |
| Settlement window | Done. `validity_seconds: 7200` in both manifests. The buyer sets `settleBy` equal to the policy `valid_until`; before this change `settleBy` stayed at 900 seconds whatever the manifest said. | `buyer_mcp_server.py`, `config/*.yaml` |
| Mainnet manifest | Done, with empty address and image fields until deployment. | `config/mainnet/manifest.yaml` |
| `check-arc.py` accepts chain 5042 | Done. `probe` and `verify` phases; `verify` compares deployed code with the local build and writes the deployment manifest. `ArcProbe` accepts both chains. | `scripts/check-arc.py`, `scripts/test_check_arc.py` |
| No private key on the command line | Done. `--private-key` removed from both servers. Key files must be regular files with mode `0600`. | both servers, `escrow.py` |
| Reproducible image ID | Open. Needs an x86 host with Docker: `RISC0_USE_DOCKER=1 cargo build --release --locked -p warrant-host` then `warrant-host image-id`. The cloud session cannot install the RISC Zero toolchain through its proxy. | runbook |
| Worker image digest | Done for 2026-10-13: `sha256:08e42fc4…` for the mutable tag `:pilot` (the tag moved again since 2026-10-09). Re-read it at deploy time. | `config/mainnet/manifest.yaml` |
| Gate decisions recorded | Done. | `settlement.md`, showcase README |

Phase 0 test status on 2026-10-13: the showcase suite passes (114 tests, one skipped) with
natively built `warrant-host`, `warrant-policy` and `warrant-evidence`; the probe and canary
guard tests pass. The Solidity suite and a real proof were not run in this session: it has no
`forge`, no `rzup` toolchain and no Docker daemon. Run `forge test` and the local Anvil
rehearsal (Phase 1) on the prover host before any broadcast.

### Infrastructure the canary needs

| Component | What it is | Who provides it | Status |
|---|---|---|---|
| Prover host | x86-64 Linux, Docker daemon, Foundry 1.8.4, Rust 1.98.1, `rzup` with Rust 1.88.0 and `r0vm` 3.0.5, 16 GB RAM or more. Builds the reproducible image ID, runs Phase 1, generates every settlement proof. | Team | Not arranged |
| Seller host | Public HTTPS host for the seller MCP server at `/mcp`, with the seller key file (`0600`), the io.net API key, and network access to the prover (or the prover runs on the same host). Later also the x402 front door. | Team | Not arranged |
| Buyer machine | Runs the buyer guardrail over stdio with the buyer key file and `buyer_acceptance.key`. Any machine with Python 3.12 and the built binaries. | Team | Not arranged |
| Deployer signer | Foundry keystore (`cast wallet import`) or a Ledger. Signs the two deployment transactions only. | Key holder | Not arranged |
| Wallets on Arc mainnet | Deployer (gas for two contract creations), buyer (2 USDC order plus gas), seller (gas for `accept` and `settle`). Record the balances with read-only calls before Phase 3. | Key holder | Not funded |
| Arc mainnet RPC | `https://rpc.mainnet.arc.io`, chain ID 5042. A private RPC URL goes in `WARRANT_RPC_URL`, never in a file under version control. | Public | Available |
| io.net account | API key with io-cloud access and prefunded credits; cap 1 USD per run with `--max-cost-usd 1.00`, 1 GPU, 1 replica, 1 hour, duration billing. | Team | Not arranged |
| Worker image | `ghcr.io/mnemonik-xyz/warrant-transcription` by digest, built by `.github/workflows/ionet-arc-image.yml`. No GPU run of the current image is recorded. | GHCR | Published |
| Evidence storage | A directory per environment (local, testnet, mainnet) with the deployment manifest, proofs, receipts and transcripts, outside the containers. Optional Hetzner copy. | Team | Not arranged |
| Public audio | One real public audio file as the workload. Synthetic data is disqualified. | Team | Not chosen |

### Caps

Order amount 2.00 USDC (`max_budget_usd`); lease 1 GPU, 1 replica, 1 hour; io.net 1.00 USD per
run; one order, then stop and review. Settlement window 2 hours. A broadcast with an unknown
outcome is read from the chain, never resent blind.

## Plan of 2026-10-09 (solver flow, kept for the record)

## Context
PR #12 and PR #14 (both merged 2026-10-09) polished the io.net adapter and reworked the worker image. Goal: a mainnet canary. `SolverBountyEscrow` on Arc mainnet (chain 5042, RPC verified live), one small real-proof settlement, then a separate capped io.net GPU transcription run. Decisions: mainnet canary, full local rehearsal first, prefunded io.net credits, new canary script for mainnet txs.

**Deviation from the written gates (core team decision, 2026-10-09).** The core team chose to skip the public Arc-testnet settlement rehearsal. `docs/product/settlement.md` CHAIN-1 and `showcases/ionet_arc/README.md` (milestone 5) still require it. The independent review in CHAIN-5 is also not planned. Neither gate is satisfied by this plan. Before the canary, either update those documents to record the exception, or run the gates. The canary is not a production launch.

Not implemented (state in the showcase): CCTP bridging, automatic provider payment, settlement-triggered GPU run. The GPU run is a manual step after settlement.

## Already in place
- Escrow, deploy script, solver guest/checker; CI settled a real Groth16 proof on private Anvil (`solver-bounty/ci-result.json`).
- Worker image `ghcr.io/mnemonik-xyz/warrant-transcription:pilot`: public, linux/amd64, CUDA, about 1.9 GB compressed. The manual-dispatch workflow `.github/workflows/ionet-arc-image.yml` (PR #14) builds and pushes it. The tag is mutable. On 2026-10-09 it moved from `sha256:b150032a…` to `sha256:fe86f01e322fe4bee50ce171ac7e363f54f7447bb5017ee14e8e4d298bfbc4c0` (built 15:20 UTC, after the PR #14 merge). Read the digest at deploy time and deploy by digest. A CPU-only `:pilot-arm64` image exists for local runs on Apple Silicon. The image holds no Warrant logic.
- Worker API after PR #14: `POST /job` returns a `job_id` (the audio SHA-256). Poll `GET /job/<job_id>`. `GET /jobs` lists all jobs. `GET /job` works only with zero or one job. Several uploads are accepted and run one at a time. `?language=` is supported. The worker writes JSON logs with GPU statistics and never logs the token, audio or transcript text.
- io.net deploy/destroy driver (`scripts/ionet-showcase.py`) with offline tests.

## Gaps and work items
1. **Mainnet driver `scripts/solver-canary.py`** (new, in `policy-execution`). Subcommands: `deploy`, `offer`, `accept`, `prove`, `settle`, `evidence`. Reuses the actor logic from `scripts/solver-demo.py`. Requirements: RPC URL and Foundry keystore path from args/env (never a raw key in args or sops), hard `--max-amount` cap, `--dry-run` that only simulates with `cast call`/`eth_call`, refuse chain ID other than 5042 unless `--allow-chain`, refuse `RISC0_DEV_MODE`, exclusive-create evidence files (as `ionet-showcase.py` does), no automatic retry of a broadcast tx.
2. **Real workload instance** (`solver-bounty/instance-ionet.json`): jobs, durations, units and prices taken from the real io.net estimate and a real public audio file (synthetic data is disqualified). State plainly that the checker proves a modelled cost, not the bill.
3. **Reproducible image ID**: `RISC0_USE_DOCKER=1` build of the solver guest; record `warrant-host solver-image-id`. Deploy only with that ID.
4. **Mainnet read-only probe**: extend `scripts/check-arc.py` (currently testnet-only, task/invoice) or add `check-arc-solver.py`. Two phases. Before deployment: chain ID 5042, BN254 precompiles, USDC decimals, and a real proof simulated with `eth_call` against the verifier and escrow constructors. After deployment and before any order is funded: code hashes of the deployed verifier and escrow, compared with the build and the manifest. `DeploySolver.s.sol` creates the verifier together with the escrow, so no verifier address exists before deployment.
5. **Funds**: real USDC on Arc for the bounty (target 2–5 USDC) plus a native-USDC gas reserve for deployer, buyer, seller, relayer. Route onto Arc is the user's responsibility (not in repo).
6. **Keys**: Foundry keystore or hardware wallet for the deployer; separate low-value buyer/seller/relayer keys. Mainnet keys never go into the coding-fabric sops file.
7. **Docs consistency after PR #12 and PR #14**: `execution-funding.md:15` says PayG is disabled; the adapter allows it. Use `duration` billing for the canary and update docs. Fix the `decode_result` stderr print (can echo env values such as `WARRANT_WORKER_TOKEN`).
8. **Image**: no rebuild needed. Deploy by the digest read at deploy time and record it in the manifest. The new multi-stage Dockerfile installs only `libcublas-12-3` and `libcudnn9-cuda-12` and sets `LD_LIBRARY_PATH` by hand. No GPU run of it is recorded, so the first io.net run is also its first GPU test. Keep the spend cap for that reason.
9. **Pre-mainnet self-review** (does not satisfy CHAIN-5, which requires an independent review) of checker, projection, escrow, proving build, signing boundary; record findings and resolutions in a file.
10. **Deployment manifest and evidence folder**: source commit, compiler (0.8.28 Cancun), addresses, code hashes, image ID, tx hashes, GHCR digest; separate local / mainnet evidence.
11. **Optional**: Hetzner evidence storage via a new `warrant-showcase` role copying the `universal-paywall-staging` pattern. Neither blocks the canary.

## Execution order
1. Local rehearsal on Anvil: `cargo test -p warrant-policy --locked`; `forge test`; `python3 -m unittest showcases.ionet_arc.test_pilot -v`; `scripts/solver-demo.py` with real Groth16 on an x86 Docker host (negatives: wrong image, changed journal, result substitution, replay, buyer cancellation).
2. Build `solver-canary.py`; run it end to end against local Anvil, in `--dry-run` and live-local modes.
3. Worker on CPU with real public audio (`:pilot-arm64` on Apple Silicon, or `WARRANT_DEVICE=cpu`).
4. Items 2, 3, 7, 9, 10 above, and the pre-deployment phase of item 4. Then a checkpoint, before any broadcast: the deployer, buyer, seller and relayer signers exist and sign through the chosen keystore or hardware wallet (item 6), and hold the bounty and gas balances (item 5). Check the balances with read-only calls and record them.
5. With explicit user approval at each step: deploy verifier and escrow -> post-deployment code-hash check against the manifest (item 4) -> one capped order -> prove -> settle -> one negative (duplicate settle) -> publish evidence.
6. io.net run, separately authorized: estimate -> `deploy` (duration billing, 1 GPU/1 replica/1 h, `--max-cost-usd 1.00`) -> `/health` -> `POST /job` -> poll `GET /job/<job_id>` -> collect transcript off-container -> `destroy` -> confirm status and credits.
7. Bump pins in `warrant` and `tameion` after review.

## Open before the first mainnet step (status 2026-10-09)
- Tools: the cloud session used for this plan has `cargo` and `docker` but no `forge`, `cast`, `anvil` or `rzup`. GitHub release pages returned 403 through its proxy. The rehearsal needs a machine with Foundry, RISC Zero (`rzup`, `r0vm` 3.0.5) and x86 Docker.
- Funds and signers: no USDC, gas, keystores or hardware wallet arranged.
- io.net: no API key, credit balance or spend cap arranged.
- Workload: no real instance or public audio file chosen.
- Gate documents: `settlement.md` and the `ionet_arc` README do not yet record the testnet and CHAIN-5 exception.

## Risks to state in the showcase
Scope: this is a developer showcase of verified settlement for an agent-sold schedule, plus a separate cloud run. It is not a product. There is no web app, hosted API or agent-facing interface (MCP or HTTP). The seller is a deterministic heuristic, not an LLM. The GPU run is started by hand after settlement. Do not describe it as autonomous agents or automated payment of cloud bills.

Solver proof path never settled on a public chain before; modelled cost is not the bill; settlement does not trigger the GPU run; no independent audit (self-review only); canary amounts only.

## Verification
Local tests green and negatives rejected; mainnet manifest code hashes match the build; settlement tx emits the exact result bytes; `destroy` followed by status read shows terminated and credits stopped; evidence folder separates local and mainnet.
