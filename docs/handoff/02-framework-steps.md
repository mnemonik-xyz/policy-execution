# Framework steps

Source of truth: [`../framework.md`](../framework.md). Read it first.
Each step is one pull request. Each step keeps all tests green.
A step ends when its acceptance list is true and the owner has merged it.

General rules for every step:

- Run `cargo test -p warrant-policy --locked` before each push.
- Run `forge test` on a machine with Foundry when the step touches Solidity. The cloud session cannot.
- Run `python3 verified/verify.py --verus <path> --mutations` when the step touches `verified/`.
- Record real numbers. Never copy test counts forward.
- No file moves or renames (see `README.md`).

## Step 0. Remove the buyer-signed path

See `01-cleanup.md`, C1. Wait for the owner to confirm decision 5.

## Step 1. Case trait and delivery types

Add `core/src/framework/` with:

- the `Case` trait (`Input`, `Policy`, `Extension`, `decide`),
- the `Outcome` type (`Allow`, `Ask`, `Deny`) shared by all cases,
- a `DeliverySpec` enum with the four kinds (`signed_acceptance`, `signed_attestation`, `computed_result`, `chain_observation`)
  and the planned `signed_usage`,
- the `deliverySpecHash` function (hash of the typed delivery struct, with a tag `warrant/delivery/v1`).

Port task, invoice and solver to implement `Case`. Keep their journals unchanged.
Existing entry points (`authorize`, `authorize_invoice`, `authorize_solver`) stay as thin wrappers.

Acceptance:

- All existing tests pass without edit. The golden vectors in `core/tests/identifier_vectors.rs` are unchanged.
- The three journals are byte for byte the same as before (use the existing fixtures).
- The three image IDs are unchanged. Build with `RISC0_USE_DOCKER=1` before and after, and compare.
- A new test shows that a policy without a `delivery` section is rejected by the validator of each case.
  Add the `delivery` section as an optional field in this step, and mandatory in step 2.

## Step 2. Journal v2: the common header

New header of 15 words:

```text
schemaId, policyHash, chainId, vault, token, recipient, amount,
obligationId, deliverableHash, policyVersion, validAfter, validUntil,
evidenceHash, deliverySpecHash, deliveryHash
```

Extension words follow the header:

| Case | Extension | Journal size |
|---|---|---|
| task | none | 15 words, 480 bytes |
| invoice | `poId`, `poMaxTotal`, `customer` | 18 words, 576 bytes |
| solver | `totalCost` | 16 words, 512 bytes |
| rent | same as invoice, see `03-rent-case.md` | 18 words, 576 bytes |

(Check the sizes against the code. Today they are 384, 480 and 416 bytes.)

`schemaId` is the SHA-256 of a tag that holds the case name and the journal version,
for example `warrant/journal/invoice/v2`. Put the tags in one table in `core`.

Work:

- Change `Authorization::journal()` and the case wrappers.
- Make the `delivery` section mandatory in every policy validator.
- Regenerate the fixtures and the golden vectors on purpose, in this pull request, with a new encoding version.
- New approved images for all three cases. State the new image IDs in the pull request.

Acceptance:

- Each case has a test that mutates every journal word. Today the Solidity test does this for 15 words.
- Journal sizes are checked in Rust and in Solidity.
- A test shows that a journal of one case does not decode as another case: the `schemaId` differs.
- The pull request says plainly that old approvals and old receipts are void.

## Step 3. `WarrantEscrow`

Replace `TaskEscrow`, `InvoiceEscrow` and `SolverBountyEscrow` with a base contract and per-case contracts.

Base contract:

- stores the order, including `deliverySpecHash`, fixed at `offer`;
- verifies the receipt against the pinned image ID and the journal digest;
- checks `schemaId` against the schema that the deployment pins;
- checks that the journal `deliverySpecHash` equals the stored one, and that `deliveryHash` is not zero;
- consumes the obligation once;
- pays only the recipient fixed at `offer`.

Case hooks: invoice (customer, order ceiling, `settleApproved`), solver (result publication and `ResultRequired`).

Keep: no owner, no upgrade, no policy setter, no pause, no administrative withdrawal.
Keep: `orderId = keccak256(abi.encode(chainId, escrow, customer, policyHash, poId))`.
Do not undo the customer binding. It closes order squatting.

The merged specification (#25) and the closed pull request #19 describe a proof-only escrow
(`ProofInvoiceEscrow`, `ProofInvoiceFactory`) with shared replay history across images.
Read #19 (branch `codex/proof-only-invoice-assurance`) before you design the base contract.
Decide with the owner whether the factory design is part of `WarrantEscrow`.
The factory was never merged.

Acceptance:

- A Solidity test settles with a receipt whose `deliverySpecHash` differs from the order. It must revert.
- A Solidity test settles with the wrong `schemaId`. It must revert.
- All journal-mutation tests pass for every case.
- `testOrderIdMatchesTheGoldenVector` runs and passes (it has never run).
- A cross-language fixture keeps Rust and Solidity in agreement on every journal.

## Step 4. One guest crate

Merge `methods/guest`, `methods/invoice-guest` and `methods/solver-guest` into one crate that builds one image per case from shared code.
The case is a build parameter, not an input. Adding a case adds an image. It never changes another case's image.

Keep the crate name and the folder name `methods` for now. The owner froze renames.

Acceptance:

- Three images (four with rent in step 8). Each image ID is recorded in the release notes through `core-manifest.json`.
- Each image ID is the same on two Docker builds on two runs.
- `warrant-host image-id <case>` prints the right ID.
- A guest for one case cannot produce a journal with another case's `schemaId`.

## Step 5. Generic evaluator and the Verus proof

Today `verified/src/lib.rs` has a closed `Rule` enum. Make the evaluator generic over a case atom set:
the evaluator takes a function from an atom to a three-valued result.
Verus proves the evaluator once for any atom function that meets its contract.

Fallback if Verus does not accept the generic proof: one closed atom enum for all cases. Each case allows a subset, as the task path does now.

Rules:

- The grammar stays monotone, without negation.
- Do not widen the claim. The proof covers the evaluator. It does not cover parsers, checkers, `authorize*`, signatures, serialization, commitments or contracts.
- Keep the mutation set. Add mutations for the new generic parts.

Acceptance:

- `verify.py --mutations` reports every obligation verified and every mutation rejected. Record the numbers. Today: 16 obligations, 14 of 14 mutations.
- The task, invoice and solver decisions are unchanged on all fixtures.
- Verus must run on a machine that has it. The cloud session has none.

## Step 6. One DSL shape

Every case policy has the same top-level shape (see `framework.md`, section 8):
`case`, `version`, `scope`, `validity`, `delivery`, `rule`.

Work:

- `deny_unknown_fields` everywhere.
- A JSON Schema per case in the repository, tested against the Rust types.
- The policy hash comes from the typed struct, not from JSON text.
- Update the templates (`templates/`) to the new shape. Do not move them.

Acceptance:

- A policy with a missing or unknown `delivery` kind is rejected, with a test for each case.
- Each template validates against its schema.
- The policy hash of a template is pinned in a test.

## Step 7. One host command set

```text
warrant-host check    <case> <input>             # native, no proof; replaces warrant-solver
warrant-host execute  <case> <input>             # guest in the zkVM, no proof
warrant-host prove    <case> <input> <receipt>
warrant-host image-id <case>
```

Remove `solver-cli` (crate `warrant-solver`) after `solver-demo.py` and the workflow use `warrant-host check`.
Update `scripts/solver-demo.py` and `.github/workflows/solver-bounty.yml`.

Optional, separate pull request: a prover service. Today `warrant-host` is a command-line tool only. It has no server code.
A prover service would wrap `prove` and `wrap` behind a job queue. It holds no key that can pay.
The owner asked on 2026-10-23 whether the prover can run as an independent server. The answer: not yet; it is a small wrapper.
Decide with the owner where it runs (seller's x86 Docker host, or a rented proving service).

Acceptance: every old subcommand has a case-based equivalent. Scripts and CI use the new names.

## Step 8. The rent case

See `03-rent-case.md`.

## Step 9. The DeFi swap

Lives in `warrant-swap`. Needs decision 4 first. The crates have no guest and no contract.
First measure the guest cycle count of the Bitcoin, EVM and Solana parsers.
If it is too large, the guest checks a quorum-signed observation, not raw chain data.
Do not start before step 8 is merged.
