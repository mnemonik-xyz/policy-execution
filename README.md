# Warrant policy interpreter

An owner approves **policy data and evidence authorities**. An agent proposes a
payment. A fixed Rust interpreter authenticates the evidence, evaluates the
policy, and emits an authorization. A RISC Zero guest proves that execution.

See [the detailed data-flow diagram](data-flow.md) for actor responsibilities,
privacy boundaries and the distinction between implemented and planned steps.

Validated on 2026-09-22: all 24 tests pass (17 native, 4 guest execution and
3 receipt verification). Verus reports 7 verified, 0 errors for the shared
evaluator; eight deliberate executable mutations fail verification. Two real
succinct receipts for different policies and recipients verify against the same
new interpreter image, taking 224 and 236 seconds locally. See the
[recorded results and proof boundary](verified/verification-results.md).
Payment integration remains pending.

Unlike the Circom prototype on `feat/warrant-proof-execution`, this version can
change rule combinations and accept dynamically chosen recipients without changing
the interpreter program. This is a typed policy DSL, not natural-language execution
or a Lean-proved specification. The rule evaluator now has a
[Verus correctness proof](verified/README.md); the surrounding authorization
pipeline remains outside that proof.

## First supported request

> Pay a registered contractor up to 100 USDC after our designated reviewer accepts
> the specified deliverable.

The owner selects the registry and reviewer public keys and approves this rule:

```json
{
  "all": [
    "accepted",
    { "amount_at_most": 100000000 },
    { "vendor_category_in": [7, 9] }
  ]
}
```

The amount is in token base units; the application must establish that the chosen
token has six decimals before displaying it as USDC. Categories are the registry's
defined IDs, not a model's guesses. Available rules are `all`, `any`,
`amount_at_most`, `vendor_category_in`, `accepted`, `deliverable_equals`, and
`recipient_equals`. Empty combinations, unknown operations, zero categories,
excessive nesting and oversized rules fail closed. The maximum is 128 nodes,
8 nested levels, 16 children per combination and 64 category entries.

## How the fixed interpreter works

`Rule` in `verified/src/lib.rs`, re-exported by `core`, is a typed expression tree.
`authorize()` validates that tree, verifies the evidence signatures and bindings,
and calls `matches()` to project authenticated facts into the shared, verified
`evaluate()` function. Only an allowed
request produces an `Authorization`. The guest reads the input, calls this same
function and commits the authorization's bytes to its public journal.

The guest binary's image ID identifies the interpreter; `policy_hash()` separately
commits to the user's rule tree, parameters, issuer keys and scope. For example,
changing an amount limit or adding an `any` branch changes the policy commitment
without changing the interpreter image. Adding a new operation changes the guest
program and requires approving a new image. The owner must review policy meaning;
the agent cannot supply a replacement evaluator under the existing image ID.

Verus proves that `evaluate()` matches the declarative rule semantics for every
rule tree and facts value. This is a source-level proof of the evaluator, not a
proof of `authorize()`, cryptographic dependencies or the owner's interpretation.
See the [verification scope and reproducible checks](verified/README.md).

Every accepted request must have authentic registry and reviewer evidence, even if
its rule does not require an affirmative `accepted` result. A policy omitting that
predicate can authorize a negatively reviewed task: owner review of rule meaning
is essential. Policy evaluation does not repair an overly permissive policy.

## Evidence and trust

The registry signs a credential containing the recipient, vendor category, payment
domain and validity interval. The reviewer signs a task-specific statement
containing the task ID, deliverable hash, recipient, amount, acceptance result,
domain and validity interval. The interpreter verifies both secp256k1 signatures
using the owner-approved keys and requires the evidence to match the request.

The statement proved is **"the specified reviewer signed acceptance and the policy
allows this payment"**, not **"the work objectively satisfies every user intent"**.
A future test-runner or document checker can replace or supplement reviewer
attestation, but it is not implemented here. The authorities must prevent the same
business obligation from being assigned multiple payable task IDs.

The owner approves the issuer keys before the agent chooses a recipient. This
permits new registered contractors without owner approval of every address. A
compromised registry or reviewer remains a source-integrity risk; proof validity
does not establish an honest issuer. Immediate credential revocation is not yet
implemented; validity windows and policy rotation are the current design controls.

## Proof flow and privacy

```mermaid
sequenceDiagram
    actor Owner
    participant Agent
    participant Sources as Registry and reviewer
    participant Prover as Owner-controlled prover
    participant Guest as Fixed policy interpreter in zkVM
    participant Verify as Receipt verifier

    Owner->>Prover: Approved policy, including trusted issuer keys
    Agent->>Sources: Obtain task-specific evidence
    Sources-->>Agent: Signed vendor credential and acceptance statement
    Agent->>Prover: Proposed payment and evidence
    Prover->>Guest: Policy, payment and signed evidence as private input
    Guest->>Guest: Verify signatures, bindings, validity overlap and policy rules
    alt Policy allows
        Guest-->>Prover: Public authorization journal
        Prover->>Prover: Generate cryptographic execution proof
        Prover->>Verify: Receipt and pinned interpreter image ID
        Verify->>Verify: Verify proof and journal binding
    else Invalid evidence or denied policy
        Guest-->>Prover: Fail without an authorization journal
    end
```

The CLI currently runs the prover and verifier locally. Separate agent/prover
services and an owner approval UI are not implemented. The prover sees the private
policy and evidence; a blockchain verifier would not need those inputs. Public
outputs include recipient, amount, task and deliverable identifiers, payment
domain, policy version/hash, validity bounds and evidence commitment. This is not
payment anonymity. Policy commitments are unsalted, so small policy spaces may be
guessable even when their parameters are omitted from the journal.

The guest image ID pins the interpreter code. The policy hash pins its input
parameters, trusted keys and rule tree. Supported policy changes produce a new
policy hash, not a new guest image. Adding new interpreter operations changes
the image and requires an explicit application upgrade.

## Build and test

Install Rust and [RISC Zero's `rzup`](https://dev.risczero.com/api/zkvm/install).
The tested component versions are:

```sh
rzup install rust 1.88.0
rzup install r0vm 3.0.5
cd warrant/policy-execution
cargo test -p warrant-policy --locked
cargo build -p warrant-host --release --locked
```

Both workspace and guest `Cargo.lock` files are included. The guest declares its
Rust 1.88 minimum and uses dependency resolution compatible with that toolchain.
Use `RISC0_BUILD_LOCKED=1` for builds that must reject guest-lock changes.

The guest pins RISC Zero's `k256`, `sha2` and `crypto-bigint` accelerator patches
to exact commits, following its [ECDSA example](https://github.com/risc0/risc0/tree/v3.0.5/examples/ecdsa/k256).
The native interpreter keeps ordinary RustCrypto implementations. Guest execution
tests compare the resulting authorizations and exercise forged-signature rejection;
this is behavioral testing, not a formal equivalence proof.

The following commands create **synthetic local fixtures**, execute the guest,
generate real succinct receipts and verify them. They move no funds and do not
demonstrate customer traction. Fixture keys are public test keys, not wallet keys.

```sh
mkdir -p artifacts
cargo run -q -p warrant-policy --example fixture > artifacts/request.json
cargo run -q -p warrant-policy --example fixture -- alternative > artifacts/alternative.json
target/release/warrant-host execute artifacts/request.json
RAYON_NUM_THREADS=4 target/release/warrant-host prove artifacts/request.json artifacts/request.receipt
RAYON_NUM_THREADS=4 target/release/warrant-host prove artifacts/alternative.json artifacts/alternative.receipt
target/release/warrant-host verify artifacts/request.receipt
cargo test -p warrant-host --release --test execution -- --test-threads=1
cargo test -p warrant-host --release --test receipts -- --ignored
```

`execute` checks guest execution but does not produce a cryptographic proof.
`prove` produces a succinct receipt, independently verifies it against the pinned
guest image and checks its journal against native evaluation. It refuses to
overwrite output receipts. `RISC0_DEV_MODE` and fake receipts are rejected.
After changing the guest, generate receipts in a fresh directory; earlier-image
receipts will fail verification. The receipt tests accept an absolute
`WARRANT_RECEIPT_DIR` containing both input JSON files and their matching receipts.
No remote proving service is configured; keep witness files private if they ever
contain real business data. The first proof may download public prover assets.

Proof segments default to at most `2^18` cycles to limit peak memory consumption.
`WARRANT_SEGMENT_PO2` accepts values from 16 through 20; lower values reduce segment
memory at the cost of more segments. Four Rayon workers are recommended for the
local commands above. These settings affect proof generation, not policy semantics
or the interpreter image. The original unoptimized run caused heavy swapping on
this machine; do not assume that proof generation has the same resource footprint
as ordinary guest execution.

## Payment integration boundary

This iteration implements the interpreter and native zkVM receipt flow. It does
**not yet submit payments, produce EVM-ready Groth16 seals, or deploy a new vault**.
The independent payment prototype is preserved on `feat/warrant-proof-execution`.

`Authorization::journal()` emits twelve Solidity-ABI words in this exact order:

```text
policyHash, chainId, vault, token, recipient, amount, taskId, deliverableHash,
policyVersion, validAfter, validUntil, evidenceHash
```

The eventual vault must check the receipt against the pinned interpreter image,
bind these words to its active policy/domain, check current block time and budget,
consume the task ID across policy versions, and transfer atomically. A verified
receipt by itself is not permission to spend: the interpreter cannot know current
chain authorization, spent budget or whether the task has already been paid.

This is not a formal proof that the interpreter matches the owner's intent or is
free of bugs. Native tests cover authentication, evidence substitution, changed
recipients, amount limits, policy composition, scope isolation, bounded policy
size, validity intersections and journal encoding. The explicit receipt tests
check different policies against one image and reject altered public outputs or
an incorrect image ID. Review and deployment-specific verification remain required.

The policy and evidence commitment encoding is length-delimited, domain-separated
bincode 1.3 serialization of typed structures followed by SHA-256. JSON formatting
does not change the commitment. Changes to that schema or serialization version
are interpreter changes and must not silently reuse an old approval.
