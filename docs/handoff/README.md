# Handoff: start here

Written 2026-10-23 for an agent that continues this work in a new session.
Read this file first. Then read the other files in this folder in order.

## Goal

Build one universal proving path in this repository. Any payment case must be built on it.
Every case has a policy DSL. Every DSL contains a **delivery check**: the proof that another
party satisfied the policy. The escrow or vault verifies the delivery check on chain.

The first new case is **rent computation**: pay per piece for rented compute.
The DeFi swap comes last.

## Files in this folder

| File | Content |
|---|---|
| `README.md` | This file: state, decisions, rules, traps |
| `01-cleanup.md` | The cleanup that is not finished |
| `02-framework-steps.md` | Steps 1 to 9 of the framework, with acceptance criteria |
| `03-rent-case.md` | The rent case and its delivery proof |

The design is in [`../framework.md`](../framework.md). It is the source of truth.
These files turn it into work packages.

## Decisions

| # | Decision | State |
|---|---|---|
| 1 | One journal header with `schemaId` and two delivery words | Approved |
| 2 | One guest crate, one image per case | Approved |
| 3 | Generic evaluator, one Verus proof. Fallback: one closed atom enum | Approved |
| 4 | Chain and escrowed asset for the DeFi swap | Deferred to step 9 |
| 5 | Fate of the buyer-signed settlement path | **Open.** See below |
| 6 | Who runs the meter and signs usage records for rent | **Open** |

Decision 5. The owner said earlier: remove the buyer-signed path (`settleSigned`, `invoice-sign`).
Pull request #25 merged on 2026-10-23. It makes the metered-payment design proof-only, with no signer key.
That agrees with the removal. Confirm once with the owner before step 0, then remove the path.

## Repositories

| Repository | Content | Local path used so far |
|---|---|---|
| `mnemonik-xyz/policy-execution` | This repository: core, guests, host, contracts, verified | `/home/user/policy-execution` |
| `mnemonik-xyz/warrant-showcases` | Showcases and the seller MCP | `/home/user/warrant-showcases` |
| `mnemonik-xyz/warrant-buyer` | Buyer wheel on PyPI (`warrant-buyer`) | `/home/user/warrant-buyer` |
| `mnemonik-xyz/warrant-bean-count-ledger` | Beancount ledger connector | `/home/user/warrant-bean-count-ledger` |
| `mnemonik-xyz/warrant-canary-deployment` | Canary driver, probes, evidence | `/home/user/warrant-canary-deployment` |
| `mnemonik-xyz/warrant-swap` | Swap crates | `/home/user/warrant-swap` |
| `mnemonik-xyz/warrant` | Designs, specs, whitepaper. Product docs go here | `/home/user/warrant` |
| `mnemonik-xyz/tameion` | Proposals. Holds `warrant` as a submodule | `/home/user/tameion` |

The repositories do not use submodules, except `tameion` and `warrant`.
Other repositories use released binaries. Each pins a release in a lock file
(`core.lock` with `scripts/fetch_core.py`).

## Pull requests on 2026-10-23

Check each state before you act.

| Pull request | Content | State |
|---|---|---|
| `policy-execution#70`, `#71`, `#72`, `#25` | Cleanup steps and the metered-payment revision | merged |
| `warrant-bean-count-ledger#1` | Ledger connector | merged. Docs are short |
| `warrant-canary-deployment#1` | Canary operations | merged |
| `warrant-canary-deployment#2` | Remove `ArcSolverProbe.t.sol`; canary plan item 13 | open |
| `warrant-swap#1` | Swap crates | **open. The swap crates are gone from `policy-execution` `main` (#72). Merge this one soon.** |
| `policy-execution` (this handoff) | `docs/handoff/` | open |

The owner merges pull requests. Never merge without being told.

## Rules of the repository

- Read `CLAUDE.md` of `tameion` (the file that the session loads) for the invariants.
  The most important ones:
  - Fields that select never come from the text or the model.
  - A checked claim can only restrict.
  - The key that executes is never the key that can change the policy.
  - A valid receipt is not permission to spend.
- Write all documents in Simplified Technical English. Sentences within 25 words,
  procedures within 20. Active voice. "Must" and "can", never "shall", "may" or "should".
  One word for one meaning.
- Do not widen the Verus claim. The proof covers the evaluator only.
- A change of the word order or of the commitment encoding is an interpreter change.
  It needs a new image and new golden vectors. Never reuse an old approval.
- Golden vectors: `core/tests/identifier_vectors.rs`. Change a value only on purpose.
- **Do not rename or move any file or crate for now.** The owner said so on 2026-10-23. See `01-cleanup.md`, C5.
- Develop on the branch that the session names. Never push to another branch.
  Open a pull request only when the owner asks.
- Mark each claim verified or unverified, with a date.

## Traps

- **Foundry cannot be installed** in the cloud session (download is blocked, HTTP 403).
  The Solidity tests have not run there. The new test `testOrderIdMatchesTheGoldenVector`
  has never run. A CI run or a local machine must run `forge test`.
- **Verus is not installed** in the cloud session. `verified/verify.py` has not run there.
- **The git relay accepts branch pushes only.** Tag pushes fail. The owner pushes tags.
- A force push is denied. To restart a branch after a squash merge, use
  `git checkout -B <branch> origin/main`, then `--force-with-lease`.
- Never run `git stash` during a merge. It loses `MERGE_HEAD`.
- Python `urllib` gets HTTP 403 from the proxy on JSON-RPC. Add a `User-Agent` header.
- Build with `RISC0_USE_DOCKER=1` for a reproducible image ID. A local build differs by machine.
- `prove` refuses to overwrite a receipt. After a guest change, use a new receipt directory.
- The directory `RFB-002-AccountsPayableAccountsReceivableAutomation ` in `tameion` ends with a space.

## Releases

- Latest core release: `core-v0.1.1`. Assets: helper binaries, host binary, contract artifacts, `SHA256SUMS`.
- `core.lock` in each consumer pins the tag, the asset hashes, the image IDs and the code hashes.
- A new core release means a new tag. The owner pushes the tag. Then update each `core.lock`.
- Open items from the release work:
  - Add `("Darwin","x86_64")` to the tools targets and to `core.lock` (Intel macOS).
  - `warrant-buyer` 0.1.1 is not published. The `0.1.0` description on PyPI has an Intel macOS claim that is wrong.
  - PyPI trusted publisher, the `pypi` environment, the tag ruleset and the default branches need owner action.
  - `DEFAULT_IMAGE_ID` in `warrant-showcases` (`escrow.py`) is the old `0xafd4ad2d…`. The task image ID in `core-v0.1.1` is `0xba0fa1e44827421998a79232e7e1c63ae85e9998ea4b47149da4290804a29716`. Check which one the deployed `TaskEscrow` pins before you change it.
  - Dependabot pull request `warrant-showcases#1` is pending.
  - The seller in `warrant-showcases` uses basic auth, but its README says "no password". Fix one of them.

## What the previous session did not do

- It did not read pull request #25 in full (it is merged now). Read `docs/product/lease-evidence.md` in full before step 8.
- It did not reorganize the docs. See `01-cleanup.md`.
- It did not bump the `warrant` and `tameion` submodule pointers.
