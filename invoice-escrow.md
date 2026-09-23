# Invoice escrow

`contracts/src/InvoiceEscrow.sol` settles invoices proven by the
[evidence checker](evidence-checker.md) against a funded purchase order (PO). It
keeps [`TaskEscrow`](task-escrow.md)'s trust model: no owner, upgrade function,
policy setter, pause authority or administrative withdrawal; one immutable token,
verifier and interpreter image ID per deployment. The difference is that one
funded order backs **many** invoices up to its ceiling, instead of one fixed
payment.

## Lifecycle

1. The buyer funds an order with `offer(policyHash, policyVersion, poId, vendor,
   maxTotal, acceptBy, settleBy)`. The full ceiling is transferred and reserved.
2. The vendor calls `accept(orderId)`. From then on the buyer cannot cancel
   before `settleBy`, so the vendor can deliver against committed funds.
3. For each invoice, a prover runs the invoice guest on the received document and
   signed credentials. Anyone relays the proof to `settle(seal, journal)`.
4. After `settleBy`, anyone can `close(orderId)`; the unpaid remainder returns to
   the buyer. An unaccepted order can be closed by the buyer at any time, or by
   anyone after `acceptBy`.

`orderId = keccak256(chainId, escrow, policyHash, poId)`. Order numbers repeat
across buyers; the policy commitment, which includes the buyer's PO key, keeps
them apart.

## What `settle` enforces

| Check | Why |
| --- | --- |
| Journal is exactly 14 words: the 12 `Authorization` words, `poId`, `poMaxTotal` | A task-escrow journal cannot be replayed here |
| Order found by `(policyHash, poId)` is Accepted and `settleBy` has not passed | Only committed orders pay |
| `policyVersion`, chain, escrow address, token match | Domain binding |
| `recipient` equals the order's vendor | The invoice cannot redirect payment |
| `poMaxTotal` equals the funded ceiling | The proven order is the funded order |
| `amount ≤ maxTotal − spent` (live state) | The checker's `po_spent` input may be stale; the contract's is not |
| Obligation ID not yet consumed, across **all** orders | An invoice pays once, even under another order |
| Nonzero amount, obligation, deliverable and evidence hashes; journal validity window | Well-formed authorization |
| Proof verifies for the pinned invoice image ID | The fixed interpreter produced this journal |

The rule tree, lexicon, credentials and PO lines stay inside the proven
evaluation; the contract does not interpret them.

## Evidence

- 13 Solidity tests, including 256 fuzz cases showing cumulative payments never
  exceed the ceiling; six deliberately removed checks are each caught.
- `testRustJournalDecodesFieldForField` decodes a journal produced by the Rust
  checker (`contracts/test/fixtures/invoice-journal.json`); a Rust test keeps that
  fixture equal to the current encoding.
- The Arc probe (`scripts/check-arc.py`) now also constructs `InvoiceEscrow` in a
  read-only Arc Testnet simulation.
- `scripts/invoice-demo.py` runs the whole flow on a local chain with a real proof.

## Limits

- **Squatting.** Anyone can fund an order under a known `(policyHash, poId)` first.
  Their funds can only pay the vendor under the same policy, so this is a denial of
  service, not theft; use order numbers that are not guessable before funding.
- **Image IDs are build-specific.** Local guest builds are not byte-reproducible
  across machines. Deploy with the image ID of the exact build that will prove.
- **Unaudited prototype.** Not covered by the Verus proof, which is about the Rust
  evaluator only.
