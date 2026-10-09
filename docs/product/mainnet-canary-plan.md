# Plan: Arc mainnet canary + io.net showcase

## Context
PR #12 (merged 2026-10-09) polished the io.net adapter and worker. Goal: a mainnet canary. `SolverBountyEscrow` on Arc mainnet (chain 5042, RPC verified live), one small real-proof settlement, then a separate capped io.net GPU transcription run. Decisions: mainnet canary (no public testnet rehearsal), full local rehearsal first, prefunded io.net credits, new canary script for mainnet txs.

Not implemented (state in the showcase): CCTP bridging, automatic provider payment, settlement-triggered GPU run. The GPU run is a manual step after settlement.

## Already in place
- Escrow, deploy script, solver guest/checker; CI settled a real Groth16 proof on private Anvil (`solver-bounty/ci-result.json`).
- Worker image `ghcr.io/mnemonik-xyz/warrant-transcription:pilot`, public, amd64, digest `sha256:b150032a8898c8e227c29abba60d75a4867b40634bdc49c9729a544dc5e87106`. Built manually (no CI job pushes it). It has no Warrant logic. Probably predates `?language=` support: do not send `?language=` unless the layer check shows it exists.
- io.net deploy/destroy driver (`scripts/ionet-showcase.py`) with offline tests.

## Gaps and work items
1. **Mainnet driver `scripts/solver-canary.py`** (new, in `policy-execution`). Subcommands: `deploy`, `offer`, `accept`, `prove`, `settle`, `evidence`. Reuses the actor logic from `scripts/solver-demo.py`. Requirements: RPC URL and Foundry keystore path from args/env (never a raw key in args or sops), hard `--max-amount` cap, `--dry-run` that only simulates with `cast call`/`eth_call`, refuse chain ID other than 5042 unless `--allow-chain`, refuse `RISC0_DEV_MODE`, exclusive-create evidence files (as `ionet-showcase.py` does), no automatic retry of a broadcast tx.
2. **Real workload instance** (`solver-bounty/instance-ionet.json`): jobs, durations, units and prices taken from the real io.net estimate and a real public audio file (synthetic data is disqualified). State plainly that the checker proves a modelled cost, not the bill.
3. **Reproducible image ID**: `RISC0_USE_DOCKER=1` build of the solver guest; record `warrant-host solver-image-id`. Deploy only with that ID.
4. **Mainnet read-only probe**: extend `scripts/check-arc.py` (currently testnet-only, task/invoice) or add `check-arc-solver.py`: chain ID 5042, BN254 precompiles, USDC decimals, deployed verifier code hash, a real proof checked with `eth_call`.
5. **Funds**: real USDC on Arc for the bounty (target 2–5 USDC) plus a native-USDC gas reserve for deployer, buyer, seller, relayer. Route onto Arc is the user's responsibility (not in repo).
6. **Keys**: Foundry keystore or hardware wallet for the deployer; separate low-value buyer/seller/relayer keys. Mainnet keys never go into the coding-fabric sops file.
7. **Docs consistency after PR #12**: `execution-funding.md:15` says PayG is disabled; the adapter allows it. Use `duration` billing for the canary and update docs. Fix the `decode_result` stderr print (can echo env values such as `WARRANT_WORKER_TOKEN`).
8. **Image**: keep `:pilot` for the canary but deploy by digest. First read `/app/worker.py` from the published layer to confirm contents. Rebuild only if the live run needs it. PR #14 (image/worker changes) is open: decide whether to wait.
9. **CHAIN-5 self-review** of checker, projection, escrow, proving build, signing boundary; record findings in a file.
10. **Deployment manifest and evidence folder**: source commit, compiler (0.8.28 Cancun), addresses, code hashes, image ID, tx hashes, GHCR digest; separate local / mainnet evidence.
11. **Optional**: GHCR build workflow with `packages: write`; Hetzner evidence storage via a new `warrant-showcase` role copying the `universal-paywall-staging` pattern. Neither blocks the canary.

## Execution order
1. Local rehearsal on Anvil: `cargo test -p warrant-policy --locked`; `forge test`; `python3 -m unittest showcases.ionet_arc.test_pilot -v`; `scripts/solver-demo.py` with real Groth16 on an x86 Docker host (negatives: wrong image, changed journal, result substitution, replay, buyer cancellation).
2. Build `solver-canary.py`; run it end to end against local Anvil, in `--dry-run` and live-local modes.
3. Worker on CPU with real public audio; read worker.py from the GHCR layer.
4. Items 2, 3, 4, 7, 9, 10 above.
5. Mainnet read-only probe. Then, with explicit user approval at each step: deploy escrow -> verify code hashes against the manifest -> one capped order -> prove -> settle -> one negative (duplicate settle) -> publish evidence.
6. io.net run, separately authorized: estimate -> `deploy` (duration billing, 1 GPU/1 replica/1 h, `--max-cost-usd 1.00`) -> `/health` -> `POST /job` -> collect transcript off-container -> `destroy` -> confirm status and credits.
7. Bump pins in `warrant` and `tameion` after review.

## Risks to state in the showcase
Solver proof path never settled on a public chain before; modelled cost is not the bill; settlement does not trigger the GPU run; no independent audit (self-review only); canary amounts only.

## Verification
Local tests green and negatives rejected; mainnet manifest code hashes match the build; settlement tx emits the exact result bytes; `destroy` followed by status read shows terminated and credits stopped; evidence folder separates local and mainnet.
