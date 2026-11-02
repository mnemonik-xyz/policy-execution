# Rent interface v1 (PE-0)

Status: proposal for the owner's review, 2 Nov 2026. Supersedes the settlement
interfaces A and B of `docs/product/lease-evidence.md` section 5. Built on the
framework (`docs/framework.md`): one journal header with `schemaId`, one escrow base
`WarrantEscrow`, one image per case. Nothing in this document is implemented.

This document fixes every encoding that two implementations must agree on: the policy
types, the signed objects, the guest input, the decision, the journal, the contract
interface, the image and schema binding, and the replay domain. An implementer reads
this document and `lease-evidence.md` 5a; where they differ, this document governs.

## 0. The three interfaces that were on the table

| | A: invoice journal as-is | B: invoice journal + delivery words | C: framework header (chosen) |
|---|---|---|---|
| Journal | The existing fifteen words; delivery implied by the rule outcome | Fifteen words + `deliverySpecHash` + `deliveryHash` | Common header with `schemaId` and the two delivery words, then case words (section 5) |
| Escrow | `ProofInvoiceEscrow`, unchanged | `ProofInvoiceEscrow` with one added check at `settle` | `WarrantEscrow`, one base for every case, checks `schemaId` and `deliverySpecHash` |
| Can the escrow check the delivery spec the buyer approved? | No | Yes | Yes |
| Can a rent journal be decoded as another case's? | Yes, by image confusion only | Yes, by image confusion only | No: `schemaId` |
| Host commands | `meter-*` | `meter-*` | `check rent`, `prove rent` |
| What it costs | A rent image | A rent image and a contract patch, both thrown away at C | New images for every case and a new contract: the framework's own migration, steps 2 and 3 |
| Where it was written | `lease-evidence.md` section 5 (29 Oct) | `lease-evidence.md` 5a as first written; pocc integration section 7 | `framework.md` sections 2, 4, 5; plan steps 2 and 3 |
| Status | Superseded | Not for production; not implemented | This document |

A fails the requirement that the proof of delivery is in the policy the buyer signed:
the contract cannot see `deliverySpecHash`. B meets it but is a dead end once C lands.
The owner chose C on 2 Nov (pocc decision 40).

## 1. Names and versions

| Name | Value | Meaning |
|---|---|---|
| Case name | `rent` | The `case` field of the policy and the build parameter of the image |
| Policy version | `1` | `policy.version` |
| Journal schema | `warrant/journal/rent/v2` | `schemaId = sha256(utf8("warrant/journal/rent/v2"))`. The escrow deployment pins it |
| Delivery kind | `signed_usage` | The only kind the rent case accepts in v1 |
| Record tag | `warrant/usage/v1` | The signing tag of a usage record |
| Batch tag | `warrant/usage-batch/v1` | The tag of `deliveryHash` |
| Note tag | `warrant/acceptance/v1` | Unchanged: a note is an `Acceptance` |
| Delivery spec tag | `warrant/delivery/v1` | The tag of `deliverySpecHash` |

A change to any value in this table is an interpreter change: new image, new fixtures,
new deployment. A journal of schema v2 never decodes under a v1 deployment.

## 2. Policy

The rent policy has the framework's shape (section 8 of `framework.md`) with the rent
fields. `deny_unknown_fields` applies. `policy_hash` is computed from the typed struct
by `warrant-policy hash`, never from the JSON text.

```rust
pub struct RentPolicy {
    pub case: CaseName,              // "rent"
    pub version: u64,                // 1
    pub scope: Scope,                // chain_id, vault, token
    pub validity: Validity,          // after, until (unix seconds)
    pub customer: Address,           // the funding buyer
    pub registry_key: PublicKey,     // signs the seller credential
    pub acceptance_key: PublicKey,   // signs notes
    pub lease_id: Hash,              // from the meter's request (pocc LeaseID)
    pub ceiling: u64,                // token base units, whole lease
    pub max_note: u64,               // token base units, one piece
    pub batch_size: u32,             // 1 to MAX_BATCH
    pub settlement_margin_s: u64,    // M of 5a.5: the seller's proving and settlement margin
    pub delivery: SignedUsage,
    pub rule: Rule,                  // atoms of the rent case
}

pub struct SignedUsage {
    pub kind: DeliveryKind,          // SignedUsage
    pub meter_key: PublicKey,        // signs usage records
    pub unit: u16,                   // 1 = instance-second, 2 = gpu-second
    pub price_per_unit: u64,         // token base units per unit
    pub tolerance: u64,              // token base units a note may exceed units x price
    pub min_coverage_bp: u16,        // creditable interval coverage floor, 0..=10000
    pub min_assurance: u8,           // 0 or 1
    pub meter_spec: Hash,            // the meter's parameter document hash
    pub service_start: u64,          // origin of the piece index; on a piece boundary
    pub service_end: u64,            // service_end - service_start is a multiple of piece_s
    pub piece_s: u64,                // 3600 for PoCC
    pub challenge_timeout_s: u64,    // 5a.5
}
```

`PublicKey` is a SEC1 compressed secp256k1 key, as every Warrant key. `Hash` is 32
bytes. `Address` is 20 bytes. Integers are little-endian in bincode, as today.

Validation (the policy validator refuses otherwise):

- `scope`, both keys, `meter_key`, `lease_id`, `meter_spec` nonzero; `ceiling >= max_note > 0`;
  `max_note >= piece_s * price_per_unit` (a full piece must be billable, 5a.6);
  `1 <= batch_size <= MAX_BATCH`; `min_coverage_bp <= 10000`; `min_assurance <= 1`;
  `piece_s > 0`; `service_end > service_start`; `(service_end - service_start) % piece_s == 0`;
  `service_start % piece_s == 0`.
- `settlement_margin_s > 0`; `validity.after <= service_start`;
  `validity.until >= deadline(last batch)` with `deadline` of 5a.5 computed from
  `service_start`, `service_end`, `piece_s`, `batch_size`, `challenge_timeout_s` and
  `settlement_margin_s`, all of them typed fields of this policy.
- `rule` valid for the rent atom set; the invoice flag off.

### 2.1 The settlement margin is a typed field

`settlement_margin_s` (the seller's proving and settlement margin, `M` in 5a.5) is a
top-level field of `RentPolicy`, so it is validated, hashed into `policy_hash` and
present in every fixture; `deadline(b)` is then computable by both sides from the
policy alone. It is not a "constants" object beside the rule: `deny_unknown_fields`
would refuse that, and a value outside the typed struct would be outside the
commitment. The margin is a commitment the seller makes at `accept` (SS-5): after the
latest allowed note arrival, queueing, zkVM proving, the Groth16 wrap, transaction
inclusion and the chain's finality must all fit inside it. The 1,800 s of the examples
is a placeholder until the rent proof is benchmarked (the cost document, section 6).

### 2.2 Hashes

- `policy_hash = hash_tagged("warrant/policy/rent/v1", bincode(RentPolicy))`.
- `deliverySpecHash = hash_tagged("warrant/delivery/v1", bincode(SignedUsage))`.
- `lease_id` is the meter's: for PoCC, `H("PoCC/poc/lease", tenant_pubkey, request_id, period.start, period.end)`.
  Warrant treats it as opaque 32 bytes.

## 3. Signed objects

| Object | Struct | Signing message | Signer |
|---|---|---|---|
| Seller credential | `VendorCredential` (unchanged) | existing | `registry_key` |
| Note k | `Acceptance` (unchanged): `scope`, `task_id = note_id(lease_id, k)`, `deliverable_hash = record_hash(k)`, `recipient`, `amount`, `accepted = true`, `valid_after`, `valid_until` | `tagged_bytes("warrant/acceptance/v1", bincode(note))` | `acceptance_key` |
| Usage record k | `UsageRecord` (below) | `tagged_bytes("warrant/usage/v1", bincode(record))` | `meter_key` |

```rust
pub struct UsageRecord {
    pub scope: Scope,
    pub lease_id: Hash,
    pub first_index: u32,
    pub last_index: u32,         // == first_index in v1
    pub window_start: u64,
    pub window_end: u64,
    pub units: u64,
    pub creditable: u64,
    pub assurance: u8,
    pub meter_spec: Hash,
    pub deliverable_hash: Hash,  // the meter's evidence hash (PoCC receipt_hash)
}
```

- `record_hash(k) = sha256(tagged_bytes("warrant/usage/v1", bincode(record_k)))`: the
  same bytes the meter key signs. A note binds to one record through it (D5).
- Signatures are 64-byte secp256k1 `r || s` (low-s) over `sha256(message)`, verified
  under the named public key with no recovery, as `core::verify` does today
  (`Signature::from_slice`). The 65-byte `r || s || v` form exists only for
  `settleSigned`, which the rent case does not have.
- `note_id(lease_id, k) = hash_tagged("warrant/note/v1", (lease_id, k))` and
  `obligation_id(lease_id, b) = hash_tagged("warrant/obligation/v1", (lease_id, b))`,
  as in `lease-evidence.md` section 5.
- Windows: `valid_until` of note k equals `deadline(batch(k))` (BG-6).

Warrant publishes byte-for-byte fixtures before any independent implementation
starts: one `RentPolicy` with its bincode bytes and `policy_hash`; its `SignedUsage`
with `deliverySpecHash`; one record in JSON, its bincode bytes, its signing message,
its 64-byte signature and its hash; one note the same way; one `RentInput` and the
journal it yields. The meter (which writes JSON and never bincode, decision 18) checks
that its JSON maps to the same bytes (DL-12). Until these fixtures exist, PE-0 is not
frozen.

## 4. Guest input and decision

```rust
pub struct RentInput {
    pub policy: RentPolicy,
    pub request: Request,                        // core::Request: scope, task_id, deliverable_hash, recipient, amount
    pub batch: u32,
    pub vendor: VendorCredential,
    pub vendor_signature: Vec<u8>,
    pub notes: Vec<(Acceptance, Vec<u8>)>,       // index order, a prefix of the batch
    pub records: Vec<(UsageRecord, Vec<u8>)>,    // same order, one per note
}
```

`request` is the existing `core::Request` (`scope`, `task_id = obligation_id(lease_id,
batch)`, `deliverable_hash` = the hash of the ordered note identifiers, `recipient`,
`amount` = the sum of the notes). It is the seller's claim; `decide` checks it against
the notes. `Authorization` is the guest's result type and never an input.

`decide` runs, in order: the nine checks of `lease-evidence.md` section 5 (policy,
request, credential, notes, amount, `deliverable_hash` of the request, validity
window, facts, evidence hash), then gates D1 to D9 of 5a.3, then the rule tree.
`Allow` yields the journal; `Ask` for a missing record; `Deny` otherwise. The Go
reference `pkg/adapter/warrant.Decide` in `mnemonik-xyz/pocc` and
`test/vectors/usage-gates.json` are the conformance cases for D1 (per record) and
D4 to D9; D1 (batch), D2, D3 and D5's hash binding are tested on the Warrant side
with the fixture of section 3.

## 5. Journal v2 for rent

Fifteen header words, then five extension words:

```text
header:    schemaId, policyHash, chainId, vault, token, recipient, amount,
           obligationId, deliverableHash, policyVersion, validAfter, validUntil,
           evidenceHash, deliverySpecHash, deliveryHash
extension: leaseId, batch, batchSize, ceiling, customer
```

| Word | Value |
|---|---|
| `schemaId` | section 1 |
| `policyHash` | 2.2 |
| `chainId`, `vault`, `token` | `policy.scope` |
| `recipient` | the seller, from the request and the credential |
| `amount` | the sum of the batch's notes |
| `obligationId` | `obligation_id(lease_id, batch)` |
| `deliverableHash` | hash of the ordered note identifiers (unchanged from section 5) |
| `policyVersion` | 1 |
| `validAfter`, `validUntil` | the intersection window of check 7 |
| `evidenceHash` | over the checker version, the credential, the notes and the records |
| `deliverySpecHash` | 2.2 |
| `deliveryHash` | `hash_tagged("warrant/usage-batch/v1", records in index order)` |
| `leaseId`, `batch`, `batchSize`, `ceiling`, `customer` | the rent extension; `batchSize` is the partition the registry pins (section 8) |

Each word is 32 bytes, ABI-encoded as the escrow decodes it (`uint256`/`bytes32`/`address`
per word, as the existing journals). A fixture mutates every word and expects the
Solidity decoder to refuse each mutation.

## 6. Contract: `WarrantEscrow`, rent hooks

`WarrantEscrow` is framework step 3. The rent case uses it with these terms and hooks.

```solidity
struct Terms {
    bytes32 policyHash;
    uint64  policyVersion;
    bytes32 obligationScope;   // lease_id: orderId = orderIdFor(msg.sender, policyHash, lease_id)
    address recipient;
    uint64  maxTotal;          // == policy.ceiling
    bytes32 deliverySpecHash;  // stored at offer, checked at settle
    uint64  acceptBy;
    uint64  settleBy;          // >= deadline(last batch)
}
function offer(Terms calldata t) external returns (bytes32 orderId);   // funds maxTotal
function accept(bytes32 orderId) external;                             // recipient only
function settle(bytes calldata seal, bytes calldata journal) external; // proof only
function close(bytes32 orderId) external;                              // after settleBy: remainder to customer
```

`settle` checks, in order:

1. Journal length for schema v2; `schemaId` equals the pinned one.
2. `verifier.verify(seal, imageId, sha256(journal))` against the pinned rent image.
3. The order: `orderId = orderIdFor(customer, policyHash, leaseId)` from the journal's
   own words; the order exists and is in state `Accepted` (not `Missing`, `Offered` or
   `Closed`).
4. Terms: `policyHash`, `policyVersion`, `chainId == block.chainid`, `vault ==
   address(this)`, `token`, `recipient`, `customer` equal the order's;
   `ceiling == order.maxTotal`; `leaseId == order.obligationScope`.
5. Delivery: `deliverySpecHash` equals the stored one; `deliveryHash != 0`.
6. Time: `block.timestamp` inside `[validAfter, validUntil]` and before `settleBy`.
7. Money: `spent + amount <= maxTotal`, checked arithmetic.
8. Replay: the factory registry's `partition[customer][leaseId]` is unset or equals the
   journal's `batchSize` (set it when unset); `consumed[customer][obligationId]` is
   false.
9. Effects: mark the obligation consumed, add `amount` to `spent`, pay `recipient`.

No `settleSigned` and no `settleApproved` exist for rent: proof only (framework
decision 5, first option, for rent). The Solidity tests settle before `accept`, after
`close`, with a journal for another order's terms, with a mismatched ceiling, with a
second `batchSize` for the same lease, and twice with the same obligation; each
reverts.

Events: `Offered(orderId, customer, recipient, maxTotal, deliverySpecHash)`,
`Accepted(orderId)`, `Settled(orderId, obligationId, amount)`, `Closed(orderId, refund)`.

`PoccAnchor` (pocc integration section 9) is a separate contract on the same chain;
the escrow never reads it. The guest never reads it in v1 either (decision 35).

## 7. Image and schema binding

- One guest crate, one image per case (framework section 6). The rent image ID is
  computed by `warrant-host image-id rent` from a reproducible build
  (`RISC0_USE_DOCKER=1 RISC0_BUILD_LOCKED=1`).
- The deployment pins `(imageId, schemaId)`. A new rent image is a new escrow from the
  same factory; the factory registry keeps `consumed[customer][obligationId]` across
  escrows so a batch never pays twice.
- `templates/rent/v1.json` is the policy template; its JSON Schema is tested against
  `RentPolicy`.

## 8. Replay domain

| Thing | Consumed once by | Where |
|---|---|---|
| The batch partition of a lease | `partition[customer][leaseId]` = the first settled journal's `batchSize`; every later settlement for that customer and lease, under any policy or escrow of the factory, must carry the same `batchSize` | Factory registry |
| A batch of a lease | `obligationId = obligation_id(lease_id, batch)`, meaningful only under the pinned partition | Factory registry, per customer |
| A note | Its index, inside its batch; a batch closes once (5a.5, SS-11) | Seller's ledger; the guest refuses a gap or a duplicate index |
| A record | Bound to its note by `deliverable_hash` | Guest, D5 |
| An order | `orderId = orderIdFor(customer, policyHash, lease_id)` | Escrow |

A later meter image, a later escrow, or a relayer cannot pay a consumed obligation
again. A different `lease_id` is a different order.

Why the partition is pinned: `obligation_id(lease_id, batch)` names a set of note
indices only together with `batch_size`. Two funded policies for the same lease with
different batch sizes would make overlapping obligations (batch 1 of size 2 and batch 3
of size 1 both contain note 3) that the registry would consume separately, so one note
could pay twice. Pinning `batchSize` per customer and lease at the first settlement
makes the partition immutable across policies, escrows and images, with one registry
write; the alternative, consuming every `note_id` separately, costs one storage write
per note per settlement. A buyer that wants a different batch size for the same
machine starts a new lease (a new `lease_id`: a new request id or period).

## 9. Migration consequences

- Rent has no deployed code or data: nothing to migrate for the rent case itself.
- Task, invoice and solver move to journal v2 under framework step 2; that migration
  is the framework's, not the rent case's. Rent starts on v2 and never has a v1.
- `lease-evidence.md` section 5 (interface A) and the first text of 5a (interface B)
  are superseded; they are not deleted, so the history of the choice stays readable.
- `pocc` changes nothing in its artifacts; its adapter already writes the record of
  section 3 as JSON and checks the typed `settlement_margin_s` and the validity
  bound.
- `warrant` compute-rent spec cites this document for settlement.

## 10. Order of work

| Step | Repository | Depends on |
|---|---|---|
| Framework 1 to 3 | policy-execution | — (in flight, owner's track) |
| PE-0 review | this document | — |
| PE-2 | `SignedUsage` type, validator, `deliverySpecHash` | Framework 1, PE-0 |
| PE-3 | `core/src/rent.rs`: `decide`, gates, journal v2 rent extension; golden vectors incl. the record fixture of section 3 | Framework 2, PE-2 |
| PE-6 | `WarrantEscrow` rent hooks, deployment on Arc testnet with the rent image; `PoccAnchor` | Framework 3, PE-3 |
| PC-3, PC-5 | pocc: hourly challenge/answer loop; seller ledger and state machine (DL-11) | PE-0 |
| PC-4 | pocc: Arc anchor | PE-6 |
| Live run with proof settlement | both | all above |

## 11. Acceptance

- The fixtures of sections 3 and 5 exist and the Solidity test mutates every word.
- `warrant-host check rent` decides `test/vectors/usage-gates.json` exactly as
  `pkg/adapter/warrant.Decide` does.
- `warrant-policy hash templates/rent/v1.json` is stable across machines.
- An end-to-end run on Arc testnet settles one batch of real PoCC records through
  `WarrantEscrow` with a proof, and refuses the same batch a second time.
