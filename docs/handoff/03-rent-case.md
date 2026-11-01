# The rent case

Rent computation is the first new target case. A buyer pays per piece for rented compute.
A piece can be a GPU job, an hour of a virtual server or a gigabyte.

## Sources

| Source | Content | State |
|---|---|---|
| [`docs/product/lease-evidence.md`](../product/lease-evidence.md) | The specification of the metered payment family | merged, with the #25 revision |
| Pull request #25 | Revision: proof for every batch, no signer key. Its text is now `docs/product/lease-evidence.md` on `main` | merged 2026-10-23 |
| Pull request #19, branch `codex/proof-only-invoice-assurance` | `ProofInvoiceEscrow` and `ProofInvoiceFactory` | closed, not merged |
| [`../framework.md`](../framework.md), section "Rent computation" | The delivery proof | this branch |
| `warrant#13` | The compute marketplace, the first consumer | in the `warrant` repository |

Read `lease-evidence.md` in full before you write code. The previous session read sections 1 to 3, 5, 8, 9, 10 and 12 only.

## What exists in the specification

- **The note.** The existing `Acceptance` struct, signed by the buyer's acceptance key. Fields: `scope`, `task_id` (derived from `lease_id` and an index),
  `deliverable_hash` (the payload hash of the 402 challenge), `recipient`, `amount` (at most the per-note cap), `accepted`, `valid_after`, `valid_until`.
- **The 402 challenge.** The seller demands a note per request, in the x402 style, with the scheme `warrant-note`.
- **Batches.** The seller settles notes in batches. One batch is one obligation: `obligation_id(lease_id, batch)`. A note index belongs to one batch.
- **The module** `core/src/meter.rs`: `MeterPolicy`, `MeterInput`, `MeterAuthorization`, `authorize_meter`. It reuses `Acceptance`, `VendorCredential`
  and the 15-word invoice journal layout. The specification lists nine ordered checks.
- **Settlement.** A proof for every batch, on the proof-only escrow. Nothing changes in the escrow ABI in the old design.

## What is missing: the delivery proof

The old design says: "It does not know whether the service ran. Nobody can check that."
A signed note proves that the buyer asked for a piece and agreed to pay. It does not prove delivery.
The owner wants a delivery proof in every case.

### Design: `signed_usage`

The policy gets a `delivery` section:

```json
"delivery": {
  "kind": "signed_usage",
  "meter_key": "<SEC1 compressed secp256k1 key>",
  "unit": "gpu-second",
  "price_per_unit": 1000,
  "tolerance": 0
}
```

The owner approves `meter_key`. It never comes from the agent or a model.

A **usage record** is a typed struct, signed by `meter_key`:

| Field | Content |
|---|---|
| `scope` | chain, escrow, token |
| `lease_id` | the lease the record belongs to |
| `first_index`, `last_index` | the note indices that the record covers |
| `units` | the metered units in that range |
| `window_start`, `window_end` | the time window of the metering |
| `deliverable_hash` | hash of the ordered note identifiers, or of the metered artifact |

The signing message has its own tag, `warrant/usage/v1`, so a usage record cannot be a note.

The guest, after the existing gates, checks:

1. The signature of each record under `meter_key`.
2. The scope and the `lease_id` equal the policy's.
3. The records cover the batch indices exactly, with no gap and no overlap.
4. The sum of the note amounts is at most `units * price_per_unit + tolerance`.
5. The record window fits inside the validity window of the policy.

Results:

- A missing record: `Ask`.
- A bad signature, a wrong lease, a gap, or a billed amount above the metered amount: `Deny`.

The journal commits `deliverySpecHash` (hash of the `delivery` section) and `deliveryHash` (hash of the usage records).
The escrow checks that `deliverySpecHash` equals the value that the buyer fixed at `offer`.

### What it proves

It proves that the key the owner approved signed a record of delivery, and that the buyer was not billed for more than the record shows.
It does not prove that the meter was honest. The meter key is a new trust assumption. State it in the trust table of the specification.

### Decision 6 (proposed answer, 2026-10-29): PoCC, run by the buyer

PoCC (`mnemonik-xyz/pocc`) is the meter. Its verifier credits seconds from heartbeats
that an agent in the rented machine signs and a notary anchors on the escrow chain.
The verifier is public code over public inputs. In the first implementation the buyer
runs it, and `meter_key` is the buyer's guardrail key. A later step runs the verifier
inside the guest, and the key disappears. Specification: `docs/product/lease-evidence.md`,
section 5a, and `docs/warrant-integration.md` in `mnemonik-xyz/pocc`.

The delivery section gains `min_uptime_bp`, `min_assurance` and `meter_spec`. The usage
record gains `assurance` and `meter_spec`. Both are in section 5a.

### Decision 6 (the options that were on the table)

| Option | Trust | Cost |
|---|---|---|
| The compute provider signs usage | The buyer trusts the provider | The provider must sign records. No provider does today. |
| An independent metering service signs usage | The buyer trusts that service | A new service to build and run |
| The seller signs, the buyer countersigns | Two parties agree | Needs the buyer online, as the old signed path did |
| `computed_result` instead (solver style) | The checker, not a party | Only for work with a checkable output |

The owner chose the buyer-run PoCC verifier on 2026-10-29. The key is named: the
buyer's guardrail key.

## Work packages

### R1. Specification update

Edit `docs/product/lease-evidence.md`:

- add the `delivery` section and the usage record;
- change "Nobody can check that" to state exactly what the meter key attests;
- add the meter key to the trust assumptions and the attack table;
- keep the text in Simplified Technical English.

### R2. `core/src/meter.rs`

Implement `Case` for rent (framework step 1). Add `UsageRecord`, its signing message and `authorize_meter`.
The nine checks of the specification stay. The delivery check runs before the rule tree. It is mandatory, not a rule atom.

Tests:

- Every gate has a test. A removed-check experiment fails for each gate (acceptance ME-1).
- The delivery gates: missing record, bad signature, wrong lease, gap, overlap, overbilling, window outside validity.
- A fixture with a valid batch and one with each failure.

### R3. Guest and host

- Add the rent image in the guest crate (framework step 4).
- `warrant-host` commands: `check rent`, `execute rent`, `prove rent`, `image-id rent` (framework step 7).
- No `meter-sign`. Settlement is by proof only.

### R4. Contracts

- Deploy on the proof-only escrow. This needs the design of framework step 3 and the decision on #19.
- The journal is the invoice layout plus the delivery words. The Solidity test mutates every word.
- The escrow checks `deliverySpecHash`.

### R5. Template

`templates/meter-v1.json` with ceiling, per-note cap, batch size, meter key, unit and price as parameters.
Add a file; do not move existing ones.

### R6. Seller side and buyer side (other repositories)

- The 402 middleware and the settlement worker belong to the seller service in `warrant-showcases` or the marketplace in `warrant`.
- The guardrail challenge handler belongs to `warrant-buyer`.
- The settlement worker proves each batch in the background and retries on prover failure.
  It stops accepting notes at a margin before `settleBy`.
- Run the prover on an x86 Docker host (`scripts/prover-vm.sh`) or a rented service. Decide with the owner.

## Acceptance criteria

From #25, kept:

- ME-1: every gate of `authorize_meter` has a test, and a removed-check experiment fails for each.
- ME-2: the meter journal follows the shared layout. The Solidity test mutates every word.
- ME-3: `settle` with a receipt pays one batch. A second settlement, a reused index and a second escrow of the same factory are rejected.
- ME-4: a note with a future `valid_after` is rejected until that time.
- ME-5: the meter image ID is recorded from a `RISC0_USE_DOCKER=1` build before any deployment.
- ME-6: a metered path returns the 402 challenge, serves against a valid note and refuses a reused index.
- ME-7: the settlement worker proves a full and a partial batch, retries after a prover failure and stops at its margin. Mock the prover and the verifier.
- ME-8: proving time, CPU seconds, peak memory and cost for one full batch are measured on the x86 prover before the batch size is chosen.

New:

- DL-1: a batch without a usage record gives `Ask`, and no journal exists.
- DL-2: a batch that bills more than `units * price_per_unit + tolerance` gives `Deny`.
- DL-3: a usage record signed by a key other than `meter_key` gives `Deny`.
- DL-4: the escrow rejects a receipt whose `deliverySpecHash` differs from the order's.
- DL-5: the written trust table names the meter key and says what happens if it fails.
- DL-6: a record below `min_uptime_bp` or `min_assurance` gives `Deny`.
- DL-7: a PoCC receipt from a live run against an escrow order (`test/live/` in `mnemonik-xyz/pocc`) turns into a usage record that `warrant-host check rent` accepts.

## Risks

- **The meter key is the buyer's.** A buyer can under-credit. The seller's loss is one piece, and the under-credit is visible to anyone who re-runs the PoCC verifier. The guest-run verifier removes the key.
- **Dependency on unmerged work.** #19 (the proof-only escrow and the factory) is closed and not merged. The merged specification depends on it.
- **Proving cost.** One measured proof took about 445 s for the succinct proof and about 80 s for the wrap on a Mac with four workers.
  ME-8 must measure the x86 cost before the batch size is set.
- **Single point.** A stolen note key spends the ceiling on service to the fixed recipient. The loss is bounded by the ceiling and the deadline. Keep orders small.
- **The metered checker is tested, not proved.** Like the invoice and solver checkers, it is outside the Verus claim.
