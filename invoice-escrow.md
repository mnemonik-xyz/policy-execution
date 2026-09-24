# Invoice escrow

`contracts/src/InvoiceEscrow.sol` settles invoices authorized by the
[evidence checker](evidence-checker.md) against a funded purchase order (PO). It
keeps [`TaskEscrow`](task-escrow.md)'s trust model: no owner, upgrade function,
policy setter, pause authority or administrative withdrawal; one immutable token,
verifier and interpreter image ID per deployment. Everything else is chosen by
the buyer per order, at `offer`, and accepted by the vendor as is. One funded
order backs **many** invoices up to its ceiling, and each invoice settles in one
of three ways, all to the order's accepted vendor:

- **Proof** (`settle`): a RISC Zero receipt for the invoice image, any amount.
  Minutes to produce on a CPU.
- **Signature** (`settleSigned`): the signing service the buyer named for this
  order runs the same interpreter natively and signs the journal. Sub-second, but
  only below the order's `proofThreshold` and within its `signerAllowance`. The
  buyer can `revokeSigner(orderId)` at any time; that only tightens, since proofs
  and approvals still settle.
- **Buyer approval** (`settleApproved`): when the interpreter cannot decide (Ask),
  the buyer pays the invoice on their own authority. Same vendor, same ceiling,
  same deadline, same replay protection; the payment is marked as approved, not
  proven, in the `Paid` event.

## Lifecycle

1. The buyer funds an order with `offer(Terms)`: policy hash and version, order
   number, vendor, ceiling, and the signer terms for this order: `signer`
   address, `signerAllowance` (how much may settle on signatures alone) and
   `proofThreshold` (amounts at or above it need a proof or approval). A zero
   signer means proofs and approvals only; a signer needs a nonzero allowance
   and threshold, and cannot be the vendor. The full ceiling is transferred and
   reserved.
2. The vendor calls `accept(orderId)`. From then on the buyer cannot cancel
   before `settleBy`, so the vendor can deliver against committed funds.
3. For each invoice, one of: the signing service runs `warrant-host invoice-sign`
   and anyone relays `settleSigned(journal, signature)`; a prover runs the invoice
   guest and anyone relays `settle(seal, journal)`; or, for an invoice the checker
   left undecided, the buyer calls `settleApproved(orderId, obligationId, amount,
   documentHash)`. The first two take the same 14-word journal and the same
   checks; only the authenticator differs.
4. After `settleBy`, anyone can `close(orderId)`; the unpaid remainder returns to
   the buyer. An unaccepted order can be closed by the buyer at any time, or by
   anyone after `acceptBy`.

`orderId = keccak256(chainId, escrow, policyHash, poId)`. Order numbers repeat
across buyers; the policy commitment, which includes the buyer's PO key, keeps
them apart.

## The signing service

`warrant-host invoice-sign key.hex policy.json rpc-url request.json output.json`
is the buyer-run service behind the signature path. It is built so that the
untrusted agent cannot feed it anything the checker does not re-verify:

- **What the agent sends** is a `SignRequest`: the invoice bytes, its line
  claims, the vendor credential and purchase order with their signatures, and the
  optional reviewer acceptance. The type rejects unknown fields, so a request that
  carries a `policy` or a `po_spent` value fails to parse.
- **What the service holds** is the policy file and the signing key.
- **What the service reads from the escrow**, over JSON-RPC at signing time, is
  the order: its state, policy hash and version, recipient, signer, spend, signer
  allowance and spend, proof threshold, deadline and revocation flag.

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent (untrusted)
    participant Service as Signing service (buyer-run)
    participant Chain as InvoiceEscrow on Arc
    actor Vendor
    Agent->>Service: SignRequest: document, claims, credentials
    Service->>Service: parse request (policy or po_spent fields rejected)
    Service->>Service: load own policy, compute policyHash and orderId
    Service->>Chain: eth_chainId, order(orderId), latest block
    Chain-->>Service: state, recipient, signer, spent, allowance, threshold, settleBy
    Service->>Service: refuse unless accepted, policy matches, signer is this key, not revoked, before deadline
    Service->>Service: authorize_invoice(policy, request, po_spent = order.spent)
    alt Allow, amount below threshold and within allowance
        Service-->>Agent: journal + signature
        Agent->>Chain: settleSigned(journal, signature)
        Chain->>Chain: 14 journal checks, threshold, allowance, ECDSA.recover == order.signer
        Chain-->>Vendor: USDC
    else Allow, but a proof is needed
        Service-->>Agent: refused: amount at or above threshold or over allowance
    else Ask
        Service-->>Agent: no signature; ask record (reasons, obligationId, documentHash, payable)
    else Deny or invalid
        Service-->>Agent: error, nothing written
    end
```

The service's pre-checks mirror the contract's so that a signature it produces
is one the escrow will accept; the contract remains the authority. The `spent`
it feeds the checker is the escrow's, never the agent's, so `WithinPo` is
evaluated against live state on both sides.

### Ask and buyer approval

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent (untrusted)
    participant Service as Signing service
    actor Buyer
    participant Chain as InvoiceEscrow on Arc
    actor Vendor
    Agent->>Service: SignRequest with a line nobody can label
    Service-->>Agent: ask record: UnlabeledLines([1]), obligationId, documentHash, payable
    Agent->>Buyer: escalate with the invoice and the ask record
    Buyer->>Buyer: reads the invoice; decides
    Buyer->>Chain: settleApproved(orderId, obligationId, payable, documentHash)
    Chain->>Chain: accepted order, before settleBy, msg.sender is the buyer, within ceiling, obligation unused
    Chain-->>Vendor: USDC
    Note over Chain: Paid(..., BuyerApproval, ...) — a later proof or signature for the same obligation reverts
```

Approval is bounded like every other path: it pays only the order's vendor, only
within the remaining ceiling, only before the deadline, and consumes the
obligation ID across all paths. It is not bounded by the signer allowance or the
threshold, because it is the buyer's own explicit decision with their own key.

## What settlement enforces

| Check | Why |
| --- | --- |
| Journal is exactly 14 words: the 12 `Authorization` words, `poId`, `poMaxTotal` | A task-escrow journal cannot be replayed here |
| Order found by `(policyHash, poId)` is Accepted and `settleBy` has not passed | Only committed orders pay |
| `policyVersion`, chain, escrow address, token match | Domain binding |
| `recipient` equals the order's vendor | The invoice cannot redirect payment |
| `poMaxTotal` equals the funded ceiling | The proven order is the funded order |
| `amount ≤ maxTotal − spent` (live state) | The checker's `po_spent` input may be stale; the contract's is not |
| Obligation ID not yet consumed, across **all** orders and paths | An invoice pays once, even under another order or by another route |
| Nonzero amount, obligation, deliverable and evidence hashes; journal validity window | Well-formed authorization |
| Proof verifies for the pinned invoice image ID (`settle`) | The fixed interpreter produced this journal |
| Signature recovers to the order's `signer` over `signerDigest(journal)` (`settleSigned`) | The buyer's own service produced this journal |
| `amount < proofThreshold`, `amount ≤ signerAllowance − signerSpent`, signer not revoked (`settleSigned`) | Bounds the damage of a stolen signer key to this order's terms |
| `msg.sender` is the order's customer, amount, obligation and document hash nonzero (`settleApproved`) | Only the buyer approves, and the record is complete |

`signerDigest(journal) = sha256("warrant/invoice-signer/v1" ‖ chainId ‖ escrow ‖
sha256(journal))`, computed identically by `evidence::signer_digest` in Rust, so
the checker needs no keccak dependency. Signatures are 65-byte `r‖s‖v`, low-s,
checked with OpenZeppelin `ECDSA.recover`. This is a service key, not a wallet
flow, so EIP-712 typed data is not used.

The rule tree, lexicon, credentials and PO lines stay inside the proven
evaluation; the contract does not interpret them.

## Evidence

- 25 Solidity tests, including fuzz cases showing cumulative payments never
  exceed the ceiling and signed payments never exceed the allowance; a signer
  named on one order signs nothing for another; buyer approval respects the
  ceiling, the deadline and the shared obligation registry; inconsistent signer
  terms are refused at `offer`.
- `testRustSignatureRecoversToSigner` recovers the signer from a signature the
  Rust helper produced (`contracts/test/fixtures/invoice-signature.json`).
- `testRustJournalDecodesFieldForField` decodes a journal produced by the Rust
  checker (`contracts/test/fixtures/invoice-journal.json`); a Rust test keeps that
  fixture equal to the current encoding.
- Host unit tests pin `orderIdFor`'s layout, the signer address derivation and
  the `order()` decoding the service relies on.
- The Arc probe (`scripts/check-arc.py`) constructs `InvoiceEscrow` in a
  read-only Arc Testnet simulation.
- `scripts/invoice-demo.py` runs the whole flow on a local chain: one invoice
  settled by signature (the service reading the order from the chain), an
  undecided one by buyer approval, and a third by real proof (`--signed-only`
  skips proving). It also shows the service refusing a request that smuggles
  `po_spent` and a key the order does not name.

## Limits

- **Squatting.** Anyone can fund an order under a known `(policyHash, poId)` first.
  Their funds can only pay the vendor under the same policy, so this is a denial of
  service, not theft; use order numbers that are not guessable before funding.
- **Image IDs.** Local guest builds differ across machines. Build with
  `RISC0_USE_DOCKER=1` (see the README) for a reproducible image ID, and deploy
  with that.
- **Unaudited prototype.** Not covered by the Verus proof, which is about the Rust
  evaluator only.
