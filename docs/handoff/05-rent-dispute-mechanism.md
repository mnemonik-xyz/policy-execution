# Rent dispute mechanism

Status: draft specification, 3 Nov 2026. No implementation of this mode exists.
The owner requested the two payment paths. This document proposes their exact scope.
The settlement unit and independent evidence authority remain assumptions for review.

The owner named this flow the **dispute mechanism**.
Normal settlement uses the buyer's acknowledgement. The dispute path uses a delivery proof after the response period.
"Fallback" in the technical sections means this proof-based dispute path.
Buyer silence makes that path available; it is not evidence of delivery or an admission of fault.
Resolution follows the funded policy and verified evidence. This version adds no human arbitrator or appeal process.

Base inspected: policy-execution `e4b8ac8` (includes PR #76).
Companion: pocc `docs/handoff/dispute-mechanism.md`, based on PR #1 at `110d813`.
This proposal amends [PE-0](04-rent-interface-v1.md), not existing funded orders.
PE-0 remains the specification of the previous proof-only rent mode.

## 1. Result of the correctness review

The requested flow is sound under four conditions:

1. Funding authorizes the fallback delivery policy before service starts.
2. A proof can establish that policy without the missing buyer acknowledgement.
3. The contract enforces the response period, payment limits and one shared replay check.
4. Refunds remain unavailable until the provider's proof settlement deadline passes.

Silence is not evidence of delivery. Timeout only enables the proof path.
A proof establishes the programmed checks over authenticated inputs. It cannot establish an unauthenticated physical fact.

The current rent specification fails condition 2 for a completely silent buyer.
It requires buyer-signed hourly notes and buyer-controlled meter signatures.
The existing task guest also verifies `acceptance_signature` in `core::authorize`.
Moving those checks into Solidity, as decision 46 proposes, does not remove this dependency.

There are two distinct products:

| Product | What happens when the buyer is silent? |
|---|---|
| Add a final release signature but retain all existing hourly evidence | Proof works if the provider already holds the required notes and meter signatures. It cannot recover a missing last note. |
| Pay for delivered service despite refusal to sign its receipt | Requires an independent delivery witness and a new rent decision that does not require that receipt. This is the proposed mode below. |

## 2. Scope and assumptions

The proposed mode is `buyer_ack_or_delivery_proof`.
It is an explicit exception to rent's earlier proof-only settlement choice.
It does not restore signature settlement for every framework case.

**Assumption A: preserve existing rent batches.** Each payment releases the amount for one completed service batch.
VM creation alone does not release the remaining lease balance or pay for future hours.
For payment per completed hour, use `batch_size = 1`.
Payment of the full escrow on VM handover is a different, fixed-delivery product.
That product needs a handover predicate and an explicit transfer-of-control test before this specification can cover it.

**Assumption B: an agreed independent meter supplies fallback evidence.** The buyer selects its key before funding.
Neither the buyer nor the provider can replace that key after acceptance.
A different key held by the same buyer is not an independent meter.
The provider must be able to obtain the signed evidence without a new buyer action.

This is a trusted-meter pilot. A dishonest meter can attest false delivery.
The proof checks the attestation and the policy; it does not remove trust in the meter.
PoCC Stage 0 also retains its physical-machine and sampled-liveness limitations.
It does not establish allocated GPU resources, performance, or successful access by the customer.
The listing must identify the exact delivery predicate and those limits.

An alternative is X-1: run the delivery verifier inside the guest over authenticated witnesses.
That alternative needs authenticated beacon, anchor and provider evidence. It is not implemented by this proposal.
Neither independent metering nor X-1 is treated as an already approved business choice.

## 3. Existing code and what can be reused

Source inspection and local verification: 3 Nov 2026.

| Component | Existing evidence | Reuse and remaining work |
|---|---|---|
| `contracts/src/InvoiceEscrow.sol` | `settle`, `settleSigned`, `_authorize`, `_pay`, `close` | Reuse the common authorization/payment structure. Its invoice schema, price threshold and signer allowance are not this rent protocol. |
| Same contract | Both paths use `consumed[customer][obligation]` and `spent` | Preserve cross-path exclusion. Its map is contract-local; the framework's shared factory registry remains necessary across deployments. |
| `host/src/signer.rs` | Buyer service checks policy and live order before signing | Reuse the pattern, not the invoice decoder or its field offsets. |
| `core/src/evidence.rs` | `signer_digest`, `sign_journal`, recoverable signatures | Reuse cryptographic primitives. Rent needs a separate domain and fixtures. |
| `methods/*-guest`, receipt verification infrastructure | Existing task, invoice and solver proof paths | Reuse build/prove/wrap/verify machinery. There is no `core/src/rent.rs` or implemented `WarrantEscrow` at the inspected base. |
| pocc `pkg/verify`, `pkg/adapter/warrant` | Delivery checks, record persistence, signing bridge, gate reference | Reuse record production and storage. The current signer is buyer-controlled. |
| pocc `pkg/lease` | Continuation, reservations, timeout, stop, batch closure | Extend its accounting. Today a missing note becomes loss, not a pending proof claim. |
| Decision 46 | Proposed stateless Solidity signature verifier | Not implemented at these bases. Its buyer-note requirement is unsuitable for the no-acknowledgement fallback. |

Checks run against these bases:

- `forge test --match-contract InvoiceEscrowTest`: 30 passed, including two fuzz tests with 256 runs each.
- `go test ./pkg/lease ./pkg/adapter/warrant`: both packages passed.
- The contract tests use a test journal verifier. They do not establish a real rent proof or a deployed fallback.

## 4. Flow

```mermaid
flowchart TD
    A[Buyer funds pinned terms] --> B[Provider accepts and delivers service]
    B --> C[Persist independently available delivery evidence]
    C --> D[Request buyer acknowledgement for the batch]
    D --> E{Valid acknowledgement available?}
    E -->|Yes| F[Relay acknowledgement]
    E -->|No, response period expires| G[Generate and verify delivery proof]
    F --> H[Shared bounds and consumed-obligation check]
    G --> H
    H --> I[Mark paid and transfer to fixed provider]
```

The caller can be the provider or a relayer. The recipient always comes from the funded order.
Signing alone does not move funds. A valid settlement transaction must be included on chain.
Acknowledgement remains usable after the response period, until settlement expires.
The first successful settlement wins. The other path must revert without another transfer.

The provider can prepare witness data early. The default worker starts costly proving after the response period.
The contract cannot observe when proving started. It enforces when proof settlement becomes valid.

## 5. Terms, timing and deployment binding

Use a new policy version, journal schema and guest image for this mode.
Proposed schema name: `warrant/journal/rent-ack-proof/v1`.
Do not accept an old rent or invoice journal under this schema.
The deployment pins the image and schema. All orders on it use this mode.

Keep the existing scope, recipient, customer, lease, period, price, floors and ceiling fields.
Add `ack_timeout_s > 0`. It is distinct from the hourly `challenge_timeout_s`.
Keep `settlement_margin_s = M > 0`, now measured for the actual fallback guest.
The acknowledgement signer in this first version is the funding customer account.
Delegated signer allowances and revocation are outside this version.

The contract must store the schedule, batch size and per-piece cap needed for both paths.
Those values must also be bound to the proved policy.
Define `settlementTermsHash = sha256(abi.encode(...))` over the following ordered, explicitly typed values:

```text
uint64 policyVersion, address customer, address recipient, bytes32 leaseId,
uint64 ceiling, uint64 maxPerPiece, uint64 serviceStart, uint64 serviceEnd,
uint64 pieceS, uint32 batchSize, uint64 ackTimeoutS, uint64 settlementMarginS,
uint64 acceptBy, uint64 settleBy, bytes32 deliverySpecHash
```

Scope and `policyHash` remain separately checked journal fields.
The customer is `msg.sender` at funding. It is not supplied by a relayer.
The guest recomputes this hash from its policy and order witness; the contract compares it with its stored terms.
Order-only `acceptBy` and `settleBy` come from that witness and must satisfy policy constraints.
Every multiplication, addition, index conversion and subtraction must be checked.
Reject invalid schedules, overflowing deadlines and policy amounts above the existing supported bounds.

For scheduled batch `b`, define:

```text
pieces = (service_end - service_start) / piece_s
first(b) = b * batch_size
last(b) = min(first(b) + batch_size, pieces) - 1
batch_end(b) = service_start + (last(b) + 1) * piece_s
proof_after(b) = batch_end(b) + ack_timeout_s
deadline(b) = proof_after(b) + settlement_margin_s
```

The contract derives these values. A provider timestamp cannot start or shorten the response period.
Validate `first(b) < pieces` before subtraction.
For a full batch, acknowledgement settlement requires `now >= batch_end(b)`.
Proof settlement requires `now >= proof_after(b)`.
Both require `now <= deadline(b)` and `now <= settleBy`, plus the journal validity window.
Refund requires `now > settleBy`. Equality therefore permits settlement and refuses refund.
Policy validity and seller credentials must cover the applicable proof deadline.

For a final partial batch, the acknowledgement can settle after its last completed piece.
Its proof still waits until the scheduled `proof_after(b)`.
This fixed schedule avoids an on-chain claim-registration transaction and an unauthenticated delivery timestamp.
It can delay recovery after early termination. Use smaller batches if that delay is unacceptable.
The response period is a pre-agreed schedule, not proof that an off-chain notification reached the buyer.
Workers must publish and retry notifications; late evidence does not extend the deadline automatically.

Set `settleBy >= deadline(last scheduled batch)`. Stop service before accepting liabilities that cannot fit this budget.
Measure M from evidence acquisition, queueing, proving, wrapping, submission, inclusion and finality under load.
No CPU/GPU estimate or existing 1,800-second placeholder is a measured value for this new mode.

## 6. Claim, acknowledgement and shared checks

Keep PE-0's fifteen header words. The proposed extension, in order, is:

```text
leaseId, batch, batchSize, ceiling, customer,
firstIndex, pieceCount, settlementTermsHash
```

This is a 23-word journal. Golden fixtures must fix every word's ABI type and range before implementation is frozen.
`firstIndex` must equal `first(batch)`.
`pieceCount` is positive and cannot exceed that batch's scheduled piece count.
The claim covers that exact contiguous prefix, including zero-priced outage pieces.
`deliveryHash` binds the ordered records and the closure/billing eligibility evidence.
No arbitrary caller-selected obligation identifier is accepted.
Derive `obligationId` from the existing `(leaseId, batch)` identifier rule.
Do not add the authenticator or the new schema to the replay identifier.

For this schema, `deliverableHash` equals `deliveryHash`; it no longer hashes buyer note identifiers.
`evidenceHash` commits the checker version, credential, records and independent eligibility/closure statements.
`validAfter` is at least the end of the claimed completed prefix and the policy/credential validity starts.
`validUntil` is at most the batch deadline and the policy/credential/evidence validity ends.
The guest rejects an empty intersection. The contract checks these bounds using its stored schedule and terms.
The additional proof delay remains a contract check; it does not prevent an earlier acknowledged settlement.

The acknowledgement signs this claim through EIP-712:

```text
Domain: name = "Warrant Rent", version = "1",
        chainId = block.chainid, verifyingContract = address(this)
Type: RentAcknowledgement(bytes32 orderId,bytes32 journalHash)
journalHash = sha256(exact journal bytes)
```

The wallet must display recipient, token, amount, service range and the fact that the signature authorizes payment.
It must display that the batch closes once, including any forfeited suffix of a final partial batch.
Never label this only a login, receipt download, or harmless confirmation of VM creation.

EOA signatures use a standard checked recovery implementation: reject malformed length, high-s and zero recovery.
Smart-account customers use ERC-1271 and must return the specified magic value through a static call.
The signature is checked at settlement time. Smart-account signature validity can change.
Neither signer unavailability nor invalidation disables the independent proof path.
EIP-712 domain separation does not replace the consumed-obligation check.

Proposed entry points:

```solidity
function settleAcknowledged(bytes calldata journal, bytes calldata buyerSignature) external;
function settleProven(bytes calldata seal, bytes calldata journal) external;
```

Both paths must call the same authorization and payment routines:

1. Check schema, journal length, canonical word encoding and order existence.
2. Require state Accepted and exact customer, policy, scope, recipient, lease, ceiling and terms hash.
3. Check schedule, batch partition, contiguous prefix, per-batch cap and time limits.
4. Require `amount <= pieceCount * maxPerPiece` and `spent + amount <= ceiling`.
5. Authenticate through the selected path. Proofs use the pinned image and exact journal hash.
6. Atomically consume the same factory-registry obligation, update spend, then transfer the exact amount.

Use checks-effects-interactions and reentrancy protection for every settlement and refund entry point.
Reverts must roll back consumption and spend. A zero-amount outage batch closes without an ERC-20 transfer.
Keep the batch partition pinned across all deployments and policy versions for a customer and lease.
Retain the shared factory registry when changing images or settlement modes.
The shared terminal-cutoff state is a new registry requirement alongside partition and consumption.
Deploy this mode for fresh funded leases. Do not reinterpret existing orders or concurrently fund the same lease in legacy escrows without shared replay state.

A partial-prefix settlement closes the whole batch. There is no later top-up for its suffix.
It is allowed only as a final batch of a stopped lease.
Record that terminal cutoff in shared lease state; both paths refuse later pieces beyond it.
Previously completed earlier batches remain payable until their own deadlines.
Concurrent settlement must enforce one consistent cutoff. A new lease needs a new lease identifier.
The registry must also track the highest settled index. A new cutoff cannot invalidate already settled later pieces.

**Fast-path trust:** the buyer signature directly authorizes the bounded payment.
The contract does not execute the rule tree or independently check physical delivery on that path.
A compromised buyer account can pay the fixed provider within these bounds.
This is different from the previous proof-only guarantee and must be explicit in the funded terms.

## 7. Dispute evidence and guest changes

Funding is the buyer's advance authorization to pay amounts calculated by the delivery policy.
The provider does not need a new acceptance signature for a piece after its delivery.
Do not create a dummy `Acceptance`, set `accepted = true`, or skip that check in the old guest.
Create a versioned rent input and explicit delivery-based authorization semantics.

The fallback input contains the typed policy, order terms, seller credential, records and independent meter evidence.
It also contains authenticated billing eligibility and any final stop boundary.
The meter must bind scope, policy, lease, batch, record hashes, eligible prefix and stop boundary in its signed statement.
The statement must bind the meter specification and the service period.
Freeze this statement's exact type, tag and encodings with shared fixtures before building an independent signer.

Billing eligibility is necessary because a running provisional piece is not automatically billable under the existing continuation gate.
The independent meter must observe and durably record continuation acknowledgements and stopping events as they occur.
It cannot accept an unauthenticated provider assertion that a provisional piece became billable.
For a trust-free alternative, those events need authenticated witnesses checked inside the guest.
This protocol witness is a new integration requirement, not an existing PoCC record field.

The guest must:

- Validate the policy and its hash, order terms, credential, scope, period and pinned evidence authority.
- Authenticate each record and the closure/billing statement; reject a buyer-only meter configuration for this fallback profile.
- Enforce lease-wide indices, exact windows within `service_end`, meter specification and canonical record hashes.
- Recompute creditable limits from the agreed schedule or check the approved meter's schedule-bound attestation.
- Require `units <= creditable`, valid assurance, checked arithmetic and all mandatory delivery gates.
- Compute each amount as `units * price_per_unit`, or zero when coverage or assurance is below its floor.
- Require each amount within `maxPerPiece`; use zero tolerance in this first mode.
- Include every eligible piece in order, including zero pieces, then sum amounts with checked arithmetic.
- Derive the request amount and evidence commitments; do not trust a provider-supplied total.
- Evaluate the rent rule tree with delivery-derived facts. Reject policies requiring a fresh buyer `Accepted` fact on fallback.
- Commit the new journal, including the terms hash and the exact closed prefix.

The proof cannot establish more than its evidence model permits.
Provider-controlled telemetry without an approved independent authority is insufficient for this pilot profile.
Distinct key bytes do not prove independent ownership. Independence is a disclosed operational assumption checked when both parties accept the listing.
The seller credential and meter evidence must remain available without the buyer after service delivery.
Withheld or invalid evidence results in no proof payment. Expiry is not an alternative authorization.

For the first implementation, verify independent meter signatures inside the guest.
Decision 46's Solidity verification is an optional later optimization with a separate schema and integration test.
Do not reuse its existing record-plus-buyer-note facts layout for a fallback which has no buyer notes.

## 8. Service state and reservations

Provisioning, hourly metering, the continuation timeout and resource shutdown remain operationally unchanged.
Hourly continuation messages are distinct from the batch settlement acknowledgement.
If the buyer stops responding, stop the machine under the existing exposure policy.
The new fallback can recover eligible completed service; it does not authorize unlimited continued provisioning.

Missing settlement acknowledgement changes payment status, not the amount of collateral available.
Use reservation categories with explicit meanings:

| Category | Meaning |
|---|---|
| Open | Reserved service which is running or awaiting classification |
| Held | Eligible delivered service awaiting either settlement path, including proof work |
| Settled | Finalized payment |
| Lost | A terminal unrecoverable reservation under the existing bounded accounting |
| Unresolved | Accounting that could not enter Lost because its bound was reached |

On timeout, move eligible completed service from Open to Held, not Lost.
Keep provisional unbillable service separate; it remains provider exposure.
Acknowledge and prove operate on the same Held reservation. They must not create separate reservations.
Do not free Held collateral merely because a proof failed or another path is in flight.
Release it only after final settlement or final expiry/loss classification.
Reorgs and uncertain submissions require reconciliation before a second action changes accounting.
Preserve checked sums, the ceiling invariant and the existing Lost/Unresolved overflow behavior.

## 9. Acceptance tests and deployment gates

| Test | Required result |
|---|---|
| Valid buyer acknowledgement, no proof | Pays once; emits acknowledgement authenticator |
| No buyer signature after funding, first piece delivered | Independent meter evidence can settle after timeout |
| Buyer-only evidence withheld | Explicitly no payment; not advertised as a working independent fallback |
| Valid proof just before / exactly at `proof_after` | Revert / eligible |
| Both paths at `deadline`; refund at `settleBy` | Settlement allowed subject to other checks; refund refused |
| Both paths after deadline | Revert; remaining funds refundable only after `settleBy` |
| Acknowledgement then proof; proof then acknowledgement | Second payment reverts in either order |
| Same obligation, changed amount or evidence | Still one payment; hash changes cannot bypass consumption |
| Wrong signer, chain, escrow, customer, recipient, policy, schema or image | Revert |
| Invalid ERC-1271 magic value, malformed EOA signature | Revert |
| Mutated timing terms or provider-supplied early timestamp | Revert |
| Boot record only, future service, provisional piece or unproved handover | Cannot claim completed eligible service |
| Outage, wrong hardware assurance, missing records or gaps | Zero or denial according to the pinned policy; no hidden skip |
| Final partial prefix, later suffix, later batch, concurrent cutoff | One consistent terminal cutoff; no suffix top-up |
| New image or escrow, same customer and lease | Shared partition and replay history still prevent duplicate payment |
| Forged meter record or forged billing eligibility | Deny |
| Proof queue failure, retry, buyer acknowledgement during proof | No duplicate reservation or payment |
| Transfer failure, reentrant token, zero payout | Atomic rollback / rejection / closure without transfer |
| Underpayment acknowledgement compared with proof amount | First authorized settlement closes the batch; no hidden remainder claim |

The buyer UI must warn before authorizing a reduced final payment which closes the batch.
The provider worker must reject an acknowledgement below its expected amount and use fallback instead.
A deliberately signed reduced acknowledgement can still be relayed by another holder; do not issue it as a negotiation message.

Require new Rust/Solidity/Go fixtures for terms, journal, acknowledgement and independent meter evidence.
Run real receipt verification and a funded testnet end-to-end run for both paths.
Repeat the no-buyer-response case after deleting access to all buyer signing services.
Run existing lease adversarial tests plus the new transition/race scenarios.
Do not widen the Verus claim beyond the evaluator.

## 10. Cost measurement and implementation order

The normal path avoids zkVM proving. It still pays signature verification and transaction fees.
Let `p` be the fraction of batches settled by proof:

```text
expected cost = (1 - p) * acknowledged_settlement_cost
              + p * (proof_generation_cost + proof_settlement_cost)
              + metering_storage_notification_cost + wasted_race_work
```

Do not charge Groth16 verification to the normal path.
Do not count standalone transaction overhead twice in an integrated settlement estimate.
A hostile buyer can force `p = 1`. Price and provision the service for that case too.
Measure the actual new guest; the old per-note signature cycle totals do not apply unchanged.
The GPU decision remains a capacity/latency decision until this fallback is benchmarked under concurrent load.

Implementation packages:

1. Confirm settlement unit and independent evidence profile; freeze terms, witness and signing fixtures.
2. Implement the rent decision and journal on the framework; retain existing case semantics.
3. Add both escrow entry points, shared checks, shared replay/cutoff state and boundary tests.
4. Adapt the buyer signer and provider worker; extend lease accounting and independent meter custody.
5. Benchmark fallback proof and both settlement paths; set measured deadlines and batch sizes.
6. Execute both paths on testnet, including races, cancellation, expiry and complete buyer silence.

## 11. Normative external references

Checked 3 Nov 2026:

- [EIP-712](https://eips.ethereum.org/EIPS/eip-712): typed acknowledgement signing and domain separation. Replay prevention is an application responsibility.
- [ERC-1271](https://eips.ethereum.org/EIPS/eip-1271): contract-account signature verification and the required return value.

This is a reviewable change specification, not a frozen wire interface or production approval.
