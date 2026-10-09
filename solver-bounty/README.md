# Solver bounty

Status: implementation prototype. The default demo requires a real proof.
The explicit mock mode tests contract integration only.

An agent buys a schedule that meets fixed constraints. The buyer reserves the
payment before the seller accepts. A deterministic checker replaces reviewer
acceptance. The seller can prove and settle without another buyer transaction.

## Run the showcase

Rust is sufficient for the native check:

The workspace pins native Rust 1.98.1. The guest uses RISC Zero Rust 1.88.0.

```sh
python3 scripts/solver-demo.py --native-only
```

Install Foundry and contract dependencies for the local contract demonstration:

```sh
npm ci --prefix contracts --ignore-scripts
python3 scripts/solver-demo.py --mock-settlement
```

The mock mode prints `MOCK VERIFIER`. Its result records `realProof: false`.
It must not be presented as proof-based settlement.

For real proof settlement, install the pinned RISC Zero components and start Docker:

```sh
rzup install rust 1.88.0
rzup install r0vm 3.0.5
python3 scripts/solver-demo.py
```

The script starts a loopback-only Anvil and uses its unlocked test accounts.
It never connects to a public network. Each run creates a new artifact directory.
The default run generates a succinct receipt, wraps it as Groth16, and settles it.
The script rejects fake receipts and checks the proven journal against native output.

The bundled workload and all funds are synthetic. A real workload can replace it:

```sh
python3 scripts/solver-demo.py --instance /path/to/workload.json --max-cost 1200
```

This does not establish customer traction. The reference seller is a deterministic
heuristic, not an LLM. An AI agent can call the tool or provide another schedule.
The checker and payment rules do not depend on the solver's implementation.

## Deal flow

1. The buyer publishes the instance, its hash, a cost ceiling, and the offered fee.
2. The reference seller returns a quote through a JSON subprocess interface.
3. The buyer commits the policy and reserves the entire fee in `SolverBountyEscrow`.
4. The seller checks the recorded terms and accepts them on chain.
5. The buyer makes no further transactions.
6. The seller returns a schedule for the accepted task.
7. The guest checks the instance, result, and policy, then commits an authorization.
8. A relayer submits the proof and result bytes.
9. The escrow pays the accepted seller and emits the complete result.

The JSON messages use `warrant.solver.v1`. They are a local reference protocol,
not an implementation of the A2A standard. The chain acceptance authenticates the
seller's agreement. The request board and quote channel carry no payment authority.

The demo confirms buyer cancellation rejection, result substitution rejection,
seller payment, result publication, and replay rejection.

## Scheduling specification

`instance.json` defines machines and jobs. All quantities are unsigned integers.
Time is a relative tick count. It does not use the blockchain timestamp.
The instance cost unit is separate from the bounty token's base unit.

| Field | Meaning |
|---|---|
| Machine `capacity` | Available resource units |
| Machine `price_per_unit_tick` | Cost per resource unit per tick |
| Job `units` | Constant resource use while the job runs |
| Job `durations` | One duration per machine; zero means incompatible |
| Job `release`, `deadline` | Earliest start and latest finish |
| Job `dependencies` | Sorted, unique indices of prior jobs |
| Assignment `job`, `machine`, `start` | The proposed placement |

Limits: 128 jobs, 16 machines, and 16 dependencies per job.
Jobs must have topological index order. Assignments must have exact job index order.
There is exactly one assignment per job. Jobs are non-preemptive.
Each job occupies `[start, start + duration)`.

The checker requires all dependencies to finish before the job starts.
It checks total resource use at every job start. Capacity can increase only there.
It rejects arithmetic overflow instead of accepting a wrapped time or cost.

The cost is the sum of `duration * units * price_per_unit_tick` over all jobs.
The guest requires that cost to be at most `max_cost`.
The checker proves neither optimality nor actual future machine performance.
Both parties agree to the supplied durations and prices as task inputs.

## Policy and proof

`SolverPolicy` commits to the instance hash, checker version, cost ceiling,
payment scope, validity window, version, and rule tree.
Its hash uses the `warrant/solver-policy/v1` domain.
The instance hash uses `warrant/solver-instance/v1`.
Both use the existing typed bincode and SHA-256 commitment scheme.

`authorize_solver` first checks the instance commitment and all scheduling constraints.
It checks the result hash and cost ceiling before it evaluates the rule tree.
An `Any` branch or omitted `Accepted` rule cannot bypass those checks.
There is no caller-supplied acceptance flag or cost value.

The path reuses the existing Verus-backed evaluator without changing its source.
Supported rules are `All`, `Any`, `Accepted`, `AmountAtMost`,
`DeliverableEquals`, and `RecipientEquals`.
Registry and invoice rules fail closed because their facts do not exist here.

The native tool and solver guest call the same authorization function.
The guest commits thirteen static ABI words:

```text
policyHash, chainId, vault, token, recipient, amount, taskId, deliverableHash,
policyVersion, validAfter, validUntil, evidenceHash, totalCost
```

The extra word distinguishes this receipt from the existing task and invoice formats.
The deployment pins the solver image. Existing task and invoice deployments do not
accept the new journal. A new checker requires a new approved image.

## Result delivery

The canonical schedule format contains these fields, with integers in big-endian order:

| Bytes | Content |
|---|---|
| 19 | ASCII `warrant/schedule/v1` |
| 4 | Assignment count, `u32` |
| 16 per job | Job `u32`, machine `u32`, start `u64` |

The maximum result is 2,071 bytes. Its raw SHA-256 is `deliverableHash`.
The guest derives that hash from the same schedule it checks.

`settleWithResult` requires the exact bytes and verifies the complete journal.
It reuses the existing task escrow's live authorization checks and payment logic.
`ResultPublished` includes the task, result hash, computed cost, and complete bytes.
Payment failure reverts the state change and the event.
The inherited two-argument `settle` always reverts, including through the parent ABI.

The result is public and nonexclusive. Calldata can expose it before finality,
including when a transaction fails. This protocol does not provide confidential fair exchange.
The guarantee requires timely settlement before the agreed deadline.
Leave time for proving, relaying, and transaction inclusion.

## Tools and deployment

```sh
cargo build --locked -p warrant-solver
target/debug/warrant-solver instance-hash solver-bounty/instance.json
target/debug/warrant-solver check instance.json schedule.json
target/debug/warrant-solver policy-hash policy.json
target/debug/warrant-solver prepare policy.json instance.json schedule.json TASK_ID SELLER AMOUNT input.json
target/debug/warrant-solver inspect input.json

cargo build --release --locked -p warrant-host
target/release/warrant-host solver-image-id
target/release/warrant-host solver-execute input.json
target/release/warrant-host solver-prove input.json solver.receipt
target/release/warrant-host verify solver.receipt
target/release/warrant-host wrap solver.receipt solver-groth16.receipt
target/release/warrant-host export-evm solver-groth16.receipt evm.json
```

`solver-execute` runs the guest but creates no proof. `inspect` is also native only.
The prover sees the private inputs. The receipt is not payment anonymity.

`contracts/script/DeploySolver.s.sol` deploys the pinned verifier and solver escrow.
It reads `WARRANT_TOKEN` and `WARRANT_SOLVER_IMAGE_ID`.
For a reproducible deployment image, build with `RISC0_USE_DOCKER=1`.
Construct the policy only after the escrow address is known.
No public deployment is included in the local demonstration.

## Public-chain canary driver

Status: available now. It has not run on any public chain yet.

`scripts/solver-canary.py` runs one bounty on a public chain. It uses the same
steps as the local demonstration. Each step is a separate command.

```sh
export WARRANT_CANARY_RPC=https://...        # never put the URL in arguments
export RISC0_USE_DOCKER=1                    # reproducible image ID
python3 scripts/solver-canary.py init --dir artifacts/canary-1 --amount 2000000 --max-cost 14 \
  --instance solver-bounty/instance.json --deployer account:NAME --buyer account:NAME \
  --seller account:NAME --relayer account:NAME
python3 scripts/solver-canary.py check --dir artifacts/canary-1
python3 scripts/solver-canary.py deploy --dir artifacts/canary-1 [--execute ...]
```

The order is: `init`, `check`, `deploy`, `quote`, `offer`, `accept`, `deliver`,
`prove`, `settle`, `evidence`.

Rules that the script enforces:

- A step that spends is a dry run. It broadcasts only with `--execute`,
  `--confirm-chain` and `--confirm-amount`. Both confirm values must match the state.
- The bounty cannot exceed 10 USDC (10,000,000 units). Raising the limit needs a code change.
- Keys stay in a Foundry keystore or a Ledger. The script never reads a private key.
  Unlocked accounts work only on a chain other than Arc mainnet.
- Mainnet needs four different signers.
- The script saves each transaction intent before it broadcasts. It never retries.
  A second attempt stops with an error until a person has checked the chain.
- The `deploy` step needs `RISC0_USE_DOCKER=1`. The `prove` step rejects a proof
  from any guest other than the deployed image ID.
- The `settle` step first checks that a changed result fails in simulation.
  After payment it checks the seller balance, the result event and that a replay fails.

Run `python3 scripts/test_solver_canary.py` for the guard tests. They need no chain.
The checker proves a modelled cost, not a cloud bill. Settlement starts no cloud run.

## Validation and trust limits

```sh
cargo test --locked -p warrant-policy -p warrant-solver
forge test --root contracts
cargo test --release --locked -p warrant-host --test solver_execution
python3 scripts/solver-demo.py --mock-settlement
python3 scripts/solver-demo.py
```

The solver tests cover constraint failures, exact commitments, arithmetic overflow,
policy bypass attempts, canonical encoding, and an independent capacity oracle.
Contract tests cover result publication, all journal words, bypass paths, refunds,
replay, payment rollback, and reentrancy. They use a labelled mock verifier.

The existing formal proof covers the rule evaluator. It does not cover the new
checker, authorization path, contracts, compiler, serialization, or cryptographic implementation.
The escrow assumes its pinned verifier, the chain, and a standard ERC-20 token.
Settlement remains conditional on timely inclusion. A production audit is still required.

See [validation.md](validation.md) for checks actually run on this revision.
