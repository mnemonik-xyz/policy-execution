# Canary kickoff: dev tasks and the session prompt

Status: prepared 2026-10-14 against `main` at that date. This document lists the
development work that remains before the first mainnet broadcast of the
[canary plan](mainnet-canary-plan.md), and the prompt that starts a session on it.

## 1. State of the plan items on main

| Item | State | Dev task |
|---|---|---|
| 1, canary driver | Done: `scripts/solver-canary.py` with tests | The chain guard and the evidence gate exist on PR 19's branch only. Land them as a small PR if PR 19 does not merge first. |
| 2, real workload | Not done: `solver-bounty/instance.json` is synthetic | `solver-bounty/instance-ionet.json` from a real io.net estimate and a real public audio file |
| 3, reproducible image ID | Needs the x86 Docker host | Build with `RISC0_USE_DOCKER=1`, record the ID and the builder tag |
| 4, mainnet probe | Not done: `scripts/check-arc.py` is testnet-only, task and invoice | Solver support, two phases, read-only |
| 5, funds | Yours | — |
| 6, keys | Yours | — |
| 7, docs consistency | Not done | Reject `payg` in the adapter; redact the stderr print in `decode_result` |
| 8, image | Nothing to do | Deploy by digest |
| 9, self-review | Not done | `solver-bounty/canary/self-review.md` |
| 10, manifest and evidence | Not done | Layout, schema, and the `evidence` command writes into it |
| 11, optional storage | Not needed | — |
| 12, Circle listing | Specified, follows the canary | — |
| 13, metered payment | Specified, not a gate | — |

About four engineer-days of code before the first broadcast, plus one day of review.

## 2. The session prompt

Start a fresh session in `policy-execution` and give it the text below.

```text
You are working in the policy-execution repository, on a new branch from origin/main, to prepare the Arc mainnet canary. The plan is docs/product/mainnet-canary-plan.md. Read it first, then docs/product/settlement.md and solver-bounty/README.md. Follow CLAUDE.md in the parent repository for conventions: Simplified Technical English in every document, no emoji, claims marked verified or unverified with a date.

Scope: the solver path only. SolverBountyEscrow, the solver guest, DeploySolver.s.sol, scripts/solver-canary.py. Do not touch InvoiceEscrow, the invoice or task guests, the metered payment family, or PR 19. Do not change any contract.

Hard rules:
- Never broadcast a transaction. Every canary command runs in --dry-run or simulation. The user broadcasts, with --execute and both --confirm flags, after reading the dry run.
- Never put a key in a command line, a file in the repository, or the secrets file. Signers are a Foundry keystore or a hardware wallet. Refuse to proceed if a raw key appears.
- RISC0_DEV_MODE is prohibited. A proof is real or it does not exist.
- Chain ID 5042 only, unless the user passes --allow-chain for a local rehearsal.
- Synthetic data is disqualifying. The workload instance must come from a real io.net estimate and a real public audio file, with its source recorded.

Tasks, in this order. Commit each one separately with a short subject and push the branch. Open one pull request at the end with a summary per task.
1. Real workload: create solver-bounty/instance-ionet.json from a real io.net hardware estimate obtained through the showcase adapter, with a real public audio file. Record the estimate, its time and the audio source in solver-bounty/instance-ionet.md. State that the checker proves a modelled cost, not the bill.
2. Mainnet probe: extend scripts/check-arc.py, or add scripts/check-arc-solver.py, with two phases. Before deployment: chain ID 5042, BN254 precompiles at 0x07 and 0x08, USDC decimals, a real solver proof simulated with eth_call against the verifier and escrow constructors. After deployment and before any order is funded: code hashes of the deployed verifier and escrow compared with the local build and the manifest. Read-only. Tests with mocked RPC.
3. Consistency: make the showcase adapter reject payg billing for the pilot so that it matches docs/product/execution-funding.md, and redact the provider error details that decode_result prints to stderr so no environment value can appear in a log. Tests for both.
4. Manifest and evidence: define solver-bounty/canary/ with a manifest schema that holds source commit, compiler 0.8.28 Cancun, OpenZeppelin 5.6.1, addresses, code hashes, image ID, builder tag, transaction hashes and the GHCR image digest, and separate local/ and mainnet/ evidence folders. Make scripts/solver-canary.py evidence write into that layout. Tests.
5. Self-review: write solver-bounty/canary/self-review.md covering the checker, the projection into Facts, SolverBountyEscrow, the proving build and the signing boundary. One finding per heading, each with a resolution or an explicit open item. State that this does not satisfy gate CHAIN-5, which requires an independent review.
6. Rehearsal: run the local rehearsal from the plan's execution order step 1 and 2 on Anvil and record the results in solver-bounty/canary/local/rehearsal.md: cargo test -p warrant-policy --locked, forge test, python3 -m unittest showcases.ionet_arc.test_pilot -v, scripts/solver-demo.py --mock-settlement, and solver-canary.py end to end in --dry-run and live-local modes. Record every command, its exit status and the key figures. Mark anything you could not run, and why.

Verification before the pull request: cargo test -p warrant-policy --locked, forge test, forge fmt --check src test script, python3 -m unittest discover -s scripts -p 'test_*.py', git diff --check. Report test counts as measured, never from older documents.

Stop and ask the user before: anything that needs the x86 Docker host, io.net credits above the configured cap, or any mainnet RPC call that is not read-only.
```

## 3. After the pull request

The execution order of the canary plan continues at its step 4: the checkpoint on
signers and balances, then the broadcasts with explicit approval at each step.
