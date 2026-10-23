# Framework design: one proving path for every case

Status: proposal. No code changes yet. Written 2026-10-23.

## Goal

Any case must be built on one framework. A case is one kind of payment that an agent
proposes and that a contract enforces. Four cases exist or are planned:

- task
- invoice
- solver
- swap

Each case has a policy DSL. Each DSL contains a **delivery check**: the proof that
another party satisfied the policy. The escrow or vault verifies the delivery check on chain.

## What exists today

The four cases share ideas but not code.

| Part | Task | Invoice | Solver | Swap |
|---|---|---|---|---|
| Entry point | `authorize()` | `authorize_invoice()` | `authorize_solver()` | `swap-core` pipeline |
| Rule type | `Rule` | `Rule` | `Rule` | `SwapRule` |
| Evaluator | `verified/` | `verified/` | `verified/` | `swap-verified/` |
| Guest | `methods/guest` | `methods/invoice-guest` | `methods/solver-guest` | none |
| Journal | 12 words | 15 words | 13 words | none (a signed JCS warrant) |
| Contract | `TaskEscrow`, `PolicyExecutionVault` | `InvoiceEscrow` | `SolverBountyEscrow` | none |
| Delivery check | Signed acceptance | Signed vendor attestation, signed purchase order, parsed document | Checker run on a schedule | Counterparty lock seen on chain |

Problems:

1. Each new case copies a guest, a journal layout, a host command and a contract.
2. The delivery check is not a named part. It sits in different places in each case.
3. The escrow does not know which delivery check the buyer wanted. It trusts the image ID only.
4. Swap has no proof and no contract. The policy signer signs a warrant. Nothing on chain checks it.
5. Two evaluators exist, so the Verus proof covers two copies of one idea.

## Design

### 1. A case is a declared object

A case has these parts. Each part is a Rust type or a function.

| Part | Meaning |
|---|---|
| `Facts` | The typed facts the case can know, each with provenance |
| `Atoms` | The rule atoms the case allows. They read only `Facts` |
| `Delivery` | The delivery check (section 3) |
| `Mandatory` | Checks that run before the rule tree and cannot be skipped |
| `Extension` | Extra journal words that only this case needs |

The core defines one trait. Each case implements it.

```text
trait Case {
    type Input;       // evidence and policy, as the agent sends it
    type Policy;      // owner-approved data, including the rule and the delivery spec
    type Extension;   // case words after the common header
    fn decide(input) -> Result<Outcome<Extension>, Denial>;
}
```

`Outcome` is `Allow`, `Ask` or `Deny`, as today. Only `Allow` produces a journal.

### 2. One journal header

Every journal starts with the same header. A case adds its extension words after it.

```text
schemaId, policyHash, chainId, vault, token, recipient, amount,
obligationId, deliverableHash, policyVersion, validAfter, validUntil,
evidenceHash, deliverySpecHash, deliveryHash
```

- `schemaId` is the hash of the case name and the journal version. The escrow checks it.
  A journal of one case can never be read as a journal of another case.
- `obligationId` replaces `taskId`. The trusted side derives it, as the invoice path does now.
- `deliverySpecHash` is the hash of the delivery section of the policy.
- `deliveryHash` is the hash of the evidence that satisfied the delivery check.

This changes the word order. That is an interpreter change. It needs new approved images and
new golden vectors. It must never reuse an old approval.

### 3. The delivery check

Every policy must have exactly one `delivery` section. The policy validator rejects a policy
without one. The delivery check is mandatory evidence, not a rule atom. A rule branch cannot
bypass it. This is the same rule that the invoice path applies to its attestation today.

A delivery check has a **kind**. The kind fixes which evidence the guest must authenticate.

| Kind | Evidence | Used by |
|---|---|---|
| `signed_acceptance` | A signature of an authority key that the owner approved | task |
| `signed_attestation` | A signed statement plus facts that the guest derives from a document | invoice |
| `computed_result` | A deterministic checker runs on the delivered result | solver |
| `chain_observation` | Chain state at a stated finality, from a quorum of providers | swap |

A kind can have parameters: the key, the checker version, the instance hash, the chain and
the contract pin. The `delivery` section holds them. Its hash is `deliverySpecHash`.

Rules for every kind:

- A fact that selects (key, address, chain, contract) comes from the owner-approved
  `delivery` section. It never comes from the agent or a model.
- A missing piece of delivery evidence gives `Ask`, never `Allow`.
- A bad signature or a mismatch gives `Deny`.

### 4. The escrow verifies the delivery check

The buyer states the delivery check when the order opens. The escrow stores it.

1. At `offer`, the buyer gives `deliverySpecHash` with the other order terms. The escrow stores it.
2. At `settle`, the escrow verifies the receipt against the pinned image ID.
3. The escrow requires `schemaId` to equal the schema that the deployment pins.
4. The escrow requires the journal `deliverySpecHash` to equal the stored one.
5. The escrow requires `deliveryHash` to be non-zero. The obligation is consumed once, as today.
6. The escrow pays only the recipient that the order fixes. It keeps its own state checks:
   ceiling, expiry, consumed obligation.

The result: a receipt for a different delivery check, for example a looser one, cannot settle
this order. The image ID alone did not give this. The policy hash did cover it, but only
together with the rest of the policy.

A valid receipt is still not permission to spend. The contract checks live state.

### 5. One escrow base

`TaskEscrow`, `InvoiceEscrow` and `SolverBountyEscrow` become one base contract, `WarrantEscrow`.

- The base holds the order, the receipt verification, the header checks and the payment.
- A case contract adds only hooks: how to read its extension words, and what else it checks.
  - Invoice: the customer, the purchase order ceiling, `settleApproved`.
  - Solver: result publication.
- Ownerless, with no upgrade, no policy setter and no pause. This stays as it is.
- `InvoiceEscrow` keeps `orderId` bound to the customer. This is a security fix; do not undo it.

### 6. One guest crate

One crate, `methods/guest`, holds one `main` body. It builds **one image per case** from shared code.

- Each image pins one case. The case identifier is a build parameter, not an input.
- Adding a case adds a new image. It does not change the approved image of another case.
- The shared code is the header, the delivery check and the encoding. Each case adds its `decide`.

The trade-off: a single image for all cases gives less build work. A change in one case
would then change the approved image of every case. We reject that.

### 7. One evaluator, one proof

The rule grammar stays monotone, with no negation, and the evaluator stays three-valued.
The change: `Rule` becomes generic over a case atom set.

- The evaluator takes a function from an atom to a three-valued result.
- Verus proves the evaluator once for any atom function that meets its contract.
- A case supplies its atoms. `SwapRule` and `Rule` stop being separate evaluators.
- The proof still covers the evaluator only. Parsers, checkers, signatures, serialization and
  contracts stay outside it. Do not widen that claim.

This is the highest-risk step. Verus must accept the generic proof. If it does not, fall back to
one closed atom enum for all cases. Each case then allows a subset, as the task path does now.

### 8. One DSL shape

Every case policy has the same top-level shape:

```text
{
  "case": "invoice",
  "version": 1,
  "scope": { chain_id, vault, token },
  "validity": { after, until },
  "delivery": { "kind": "signed_attestation", ... },
  "rule": { "all": [ ... atoms of this case ... ] }
}
```

- `deny_unknown_fields` everywhere, as today.
- A JSON Schema per case lives in the repository and is tested against the Rust types.
- The policy hash is computed from the typed struct, not from the JSON text.
- Templates move to `templates/<case>/`.

### 9. One host command set

`warrant-host` takes the case as an argument.

```text
warrant-host check   <case> <input>     # native, no proof (replaces warrant-solver)
warrant-host execute <case> <input>     # guest in the zkVM, no proof
warrant-host prove   <case> <input> <receipt>
warrant-host image-id <case>
```

`solver-cli` and the case-specific commands are removed.

## Swap

Swap needs the most work, because it has no guest and no contract.

| Gap | Proposal |
|---|---|
| No guest | Add a swap image. The `chain_observation` check needs the Bitcoin, EVM and Solana parsers in the guest. |
| Cost | Measure the guest cycle count first. If the parsers are too large, split the check. The guest then checks a quorum-signed observation and not the raw chain data. |
| No contract | The swap escrow is a `WarrantEscrow` case. It verifies the receipt before it releases or refunds a lock. |
| Signed warrant | The JCS warrant stays for chains that have no contract. It is then a second output of the same `decide` function. |

Open question: which chain holds the swap escrow, and what does it escrow? Decide this with the
owner before work starts. Swap is the last step, not the first.

## Invariants that must not break

Copied from the current design. The framework must keep all of them.

- Fields that select never come from text or a model. Fields that quantify can.
- A checked claim can only restrict.
- A positive claim needs evidence. The scan for denied terms is exhaustive.
- The key that executes is never the key that can change the policy.
- A valid receipt is not permission to spend.
- Only `Allow` produces an authorization.
- A change of word order or of the commitment encoding is an interpreter change and needs a new image.

## Plan

Each step is its own pull request. Each step keeps all tests green.

| Step | Work | Risk |
|---|---|---|
| 0 | Finish the cleanup: remove the buyer-signed path | Contract interface change |
| 1 | Add `core/src/framework`: `Case` trait, header, delivery types. Port task, invoice and solver with the old journals unchanged | Low |
| 2 | Add the common header and `schemaId`. New journal v2, new golden vectors and fixtures | Medium: new images |
| 3 | Add `WarrantEscrow` with the `deliverySpecHash` check. Port the three contracts | Medium: contract change |
| 4 | Merge the guests: one crate, one image per case | Medium |
| 5 | Generic evaluator and the Verus proof | High |
| 6 | One DSL shape, JSON Schemas and templates | Low |
| 7 | One `warrant-host` command set. Remove `solver-cli` | Low |
| 8 | Swap guest and swap escrow | High |

Steps 1 to 7 change no behavior of the existing cases except the journal layout in step 2.

## Decisions needed

1. Approve one journal header with `schemaId` and the two delivery words (section 2).
2. Approve one image per case from one guest crate (section 6).
3. Approve the generic evaluator with a fallback to a closed atom enum (section 7).
4. Decide the swap escrow chain and what it escrows (swap section).
5. Decide whether the buyer-signed path is removed first (step 0) or after step 3.
