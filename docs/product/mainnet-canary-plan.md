# Plan: Arc mainnet canary + io.net showcase

> Note (2026-10-23): the `showcases/` folder, with `showcases/ionet_arc` and `scripts/ionet-showcase.py`, moved to the `mnemonik-xyz/warrant-showcases` repository. Paths under `showcases/` in this document refer to that repository.

## Context
PR #12 and PR #14 (both merged 2026-10-09) polished the io.net adapter and reworked the worker image. Goal: a mainnet canary. `SolverBountyEscrow` on Arc mainnet (chain 5042, RPC verified live), one small real-proof settlement, then a separate capped io.net GPU transcription run. Decisions: mainnet canary, full local rehearsal first, prefunded io.net credits, new canary script for mainnet txs.

**Deviation from the written gates (core team decision, 2026-10-09).** The core team chose to skip the public Arc-testnet settlement rehearsal. `docs/product/settlement.md` CHAIN-1 and `showcases/ionet_arc/README.md` (milestone 5) still require it. The independent review in CHAIN-5 is also not planned. Neither gate is satisfied by this plan. The exception is recorded in `settlement.md`, section "Recorded exception for the Arc mainnet canary", on 2026-10-14. The `ionet_arc` README no longer carries the milestone list. The canary is not a production launch.

Not implemented (state in the showcase): CCTP bridging, automatic provider payment, settlement-triggered GPU run. The GPU run is a manual step after settlement.

## Already in place
- Escrow, deploy script, solver guest/checker; CI settled a real Groth16 proof on private Anvil (`solver-bounty/ci-result.json`).
- Worker image `ghcr.io/mnemonik-xyz/transcription:pilot`: public, linux/amd64, CUDA, about 1.9 GB compressed. The separate `mnemonik-xyz/transcription` repository builds and pushes it. The tag is mutable, so read the digest at deploy time and deploy by digest. A CPU-only `:pilot-arm64` image exists for local runs on Apple Silicon. The image holds no Warrant logic.
- Worker API after PR #14: `POST /job` returns a `job_id` (the audio SHA-256). Poll `GET /job/<job_id>`. `GET /jobs` lists all jobs. `GET /job` works only with zero or one job. Several uploads are accepted and run one at a time. `?language=` is supported. The worker writes JSON logs with GPU statistics and never logs the token, audio or transcript text.
- io.net deploy/destroy driver (`scripts/ionet-showcase.py`, now in the `warrant-showcases` repository) with offline tests.

## Gaps and work items
1. **Mainnet driver `scripts/solver-canary.py`** (done; on `main` since PR #18, tests in `scripts/test_solver_canary.py`). Subcommands: `deploy`, `offer`, `accept`, `prove`, `settle`, `evidence`. Reuses the actor logic from `scripts/solver-demo.py`. Requirements: RPC URL and Foundry keystore path from args/env (never a raw key in args or sops), hard `--max-amount` cap, `--dry-run` that only simulates with `cast call`/`eth_call`, refuse chain ID other than 5042 unless `--allow-chain`, refuse `RISC0_DEV_MODE`, exclusive-create evidence files (as `ionet-showcase.py` does), no automatic retry of a broadcast tx.
2. **Real workload instance** (`solver-bounty/instance-ionet.json`): jobs, durations, units and prices taken from the real io.net estimate and a real public audio file (synthetic data is disqualified). State plainly that the checker proves a modelled cost, not the bill.
3. **Reproducible image ID**: `RISC0_USE_DOCKER=1` build of the solver guest; record `warrant-host solver-image-id`. Deploy only with that ID.
4. **Mainnet read-only probe** (done 2026-10-14: `scripts/check-arc-solver.py`, phases `before` and `after`, mocked tests; not yet run against a live RPC): extend `scripts/check-arc.py` (currently testnet-only, task/invoice) or add `check-arc-solver.py`. Two phases. Before deployment: chain ID 5042, BN254 precompiles, USDC decimals, and a real proof simulated with `eth_call` against the verifier and escrow constructors. After deployment and before any order is funded: code hashes of the deployed verifier and escrow, compared with the build and the manifest. `DeploySolver.s.sol` creates the verifier together with the escrow, so no verifier address exists before deployment.
5. **Funds**: real USDC on Arc for the bounty (target 2–5 USDC) plus a native-USDC gas reserve for deployer, buyer, seller, relayer. Route onto Arc is the user's responsibility (not in repo).
6. **Keys**: Foundry keystore or hardware wallet for the deployer; separate low-value buyer/seller/relayer keys. Mainnet keys never go into the coding-fabric sops file.

   **Amendment 2026-10-14.** The user reversed the last sentence on 2026-10-14. The canary keys live in
   `infrastructure/secrets/secrets.sops.yml` of coding-fabric, under the existing age recipient. These are the keys:
   - the Foundry keystores and passwords of the deployer, buyer, seller and relayer;
   - the seller hex key, the io.net key and the registry key;
   - the basic-auth credential of the seller MCP.

   The shared age key opens the whole file. The user accepted this risk for canary amounts. The team rotates these
   keys after the hackathon. Keys never go into a command line or into a file in this repository.
7. **Docs consistency after PR #12 and PR #14** (done 2026-10-14: the adapter rejects `payg`; `decode_result` logs a length and a digest only): `execution-funding.md:15` says PayG is disabled; the adapter allows it. Use `duration` billing for the canary and update docs. Fix the `decode_result` stderr print (can echo env values such as `WARRANT_WORKER_TOKEN`).
8. **Image**: no rebuild needed. Deploy by the digest read at deploy time and record it in the manifest. The new multi-stage Dockerfile installs only `libcublas-12-3` and `libcudnn9-cuda-12` and sets `LD_LIBRARY_PATH` by hand. No GPU run of it is recorded, so the first io.net run is also its first GPU test. Keep the spend cap for that reason.
9. **Pre-mainnet self-review** (done 2026-10-14: `solver-bounty/canary/self-review.md`, one medium and four open items; does not satisfy CHAIN-5, which requires an independent review) of checker, projection, escrow, proving build, signing boundary; record findings and resolutions in a file.
10. **Deployment manifest and evidence folder** (done 2026-10-14: `solver-bounty/canary/`, `manifest.schema.json`, the `evidence` step writes `manifest.json`): source commit, compiler (0.8.28 Cancun), addresses, code hashes, image ID, tx hashes, GHCR digest; separate local / mainnet evidence.
11. **Optional**: Hetzner evidence storage via a new `warrant-showcase` role copying the `universal-paywall-staging` pattern. Neither blocks the canary.
12. **Circle marketplace listing** (added 2026-10-14): make the seller discoverable through Circle's Discovery API. The seller needs a paid x402 quote endpoint, a seller signature on the quote, an OpenAPI document and a public HTTPS host. Circle lists Arc mainnet only, so the listing names the mainnet payout wallet and the mainnet escrow from step 5. Specification: [circle-listing.md](circle-listing.md). Marketplace design: [warrant#13](https://github.com/mnemonik-xyz/warrant/pull/13). Does not block the canary settlement; it follows it.
13. **Metered payment family** (added 2026-10-14): production-readiness track L1 in the [delivery plan](delivery-plan.md). A new guest and a deployment of `InvoiceEscrow` pinned to the meter image ID, no contract change. Specification: [lease-evidence.md](lease-evidence.md). Not a canary gate. Listed here because its mainnet deployment follows the same deploy, probe and evidence steps as items 3, 4 and 10.

## Execution order
1. Local rehearsal on Anvil: `cargo test -p warrant-policy --locked`; `forge test`; `python3 -m unittest showcases.ionet_arc.test_pilot -v` (in a `warrant-showcases` clone); `scripts/solver-demo.py` with real Groth16 on an x86 Docker host (negatives: wrong image, changed journal, result substitution, replay, buyer cancellation).
2. Build `solver-canary.py`; run it end to end against local Anvil, in `--dry-run` and live-local modes.
3. Worker on CPU with real public audio (`:pilot-arm64` on Apple Silicon, or `WARRANT_DEVICE=cpu`).
4. Items 2, 3, 7, 9, 10 above, and the pre-deployment phase of item 4. Then a checkpoint, before any broadcast: the deployer, buyer, seller and relayer signers exist and sign through the chosen keystore or hardware wallet (item 6), and hold the bounty and gas balances (item 5). Check the balances with read-only calls and record them.
5. With explicit user approval at each step: deploy verifier and escrow -> post-deployment code-hash check against the manifest (item 4) -> one capped order -> prove -> settle -> one negative (duplicate settle) -> publish evidence.
6. io.net run, separately authorized: estimate -> `deploy` (duration billing, 1 GPU/1 replica/1 h, `--max-cost-usd 1.00`) -> `/health` -> `POST /job` -> poll `GET /job/<job_id>` -> collect transcript off-container -> `destroy` -> confirm status and credits.
7. Circle listing (item 12), after the mainnet escrow exists: keyless x402 trial on Arc testnet -> quote endpoint and OpenAPI on a public HTTPS host -> ask Circle whether an escrow-settled product is eligible -> submit the form with the mainnet payout wallet -> record the review outcome.
8. Metered payment family (item 13, track L1): module and tests -> reproducible image ID -> local Anvil settlement on both paths -> deployment pinned to that ID -> one capped lease on mainnet.
9. Bump pins in `warrant` and `tameion` after review.

## Open before the first mainnet step (status 2026-10-14)

Done on branch `canary/prep`: the gate exception, items 4, 7, 9 and 10. Still open: item 2
(needs `IO_NET_API_KEY` for a real estimate), item 3 and the rehearsal step 2 with a real
proof (need the x86 Docker host), funds, keys, the self-review open items S3.8, S4.1, S5.4
and S5.7, and the Arc USDC behavior probe before `offer`. Status 2026-10-09 follows.
- Tools: the cloud session used for this plan has `cargo` and `docker` but no `forge`, `cast`, `anvil` or `rzup`. GitHub release pages returned 403 through its proxy. The rehearsal needs a machine with Foundry, RISC Zero (`rzup`, `r0vm` 3.0.5) and x86 Docker.
- Funds and signers: no USDC, gas, keystores or hardware wallet arranged.
- io.net: no API key, credit balance or spend cap arranged.
- Workload: no real instance or public audio file chosen.
- Gate documents: resolved 2026-10-14. `settlement.md` records the testnet and CHAIN-5 exception.
- Circle listing (status 2026-10-14): no public HTTPS host, no Circle API key, no x402 endpoint, no OpenAPI document. Circle has not confirmed eligibility of an escrow-settled product.

## Risks to state in the showcase
Scope: this is a developer showcase of verified settlement for an agent-sold schedule, plus a separate cloud run. It is not a product. There is no web app. The proven deal has no agent-facing interface (MCP or HTTP). The hosted seller MCP on Arc mainnet (status 2026-10-15) is agent-facing, but it settles without proof. The plan seller of the proven deal is a deterministic heuristic, not an LLM. The GPU run is started by hand after settlement. Do not describe it as autonomous agents or automated payment of cloud bills.

Solver proof path never settled on a public chain before; modelled cost is not the bill; settlement does not trigger the GPU run; no independent audit (self-review only); canary amounts only.

## Verification
Local tests green and negatives rejected; mainnet manifest code hashes match the build; settlement tx emits the exact result bytes; `destroy` followed by status read shows terminated and credits stopped; evidence folder separates local and mainnet.
