# Invoice escrow

`contracts/src/InvoiceEscrow.sol` settles invoices authorized by the
[evidence checker](evidence-checker.md) against a funded purchase order (PO). It
keeps [`TaskEscrow`](task-escrow.md)'s trust model: no owner, upgrade function,
policy setter, pause authority or administrative withdrawal; one immutable token,
verifier, interpreter image ID, signer and proof threshold per deployment. One
funded order backs **many** invoices up to its ceiling, and each invoice settles
in one of two ways:

- **Proof** (`settle`): a RISC Zero receipt for the invoice image, any amount.
  Minutes to produce on a CPU.
- **Signature** (`settleSigned`): the buyer-run signing service runs the same
  interpreter natively and signs the journal. Sub-second, but only below
  `proofThreshold` and within the order's `signerAllowance`. A stolen signer key
  cannot redirect funds, because the recipient is the order's accepted vendor;
  with a complicit vendor it can pay at most the allowance, in sub-threshold
  pieces. The buyer can `revokeSigner(orderId)` at any time; that only tightens,
  since proofs still settle.

## Lifecycle

1. The buyer funds an order with `offer(policyHash, policyVersion, poId, vendor,
   maxTotal, signerAllowance, acceptBy, settleBy)`. The full ceiling is
   transferred and reserved; `signerAllowance` (0 for proofs only) caps what may
   be paid on signatures alone.
2. The vendor calls `accept(orderId)`. From then on the buyer cannot cancel
   before `settleBy`, so the vendor can deliver against committed funds.
3. For each invoice, either the signing service runs `warrant-host invoice-sign`
   and anyone relays `settleSigned(journal, signature)`, or a prover runs the
   invoice guest and anyone relays `settle(seal, journal)`. Both take the same
   14-word journal and the same checks; only the authenticator differs.
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
| Proof verifies for the pinned invoice image ID (`settle`) | The fixed interpreter produced this journal |
| Signature recovers to the immutable signer over `signerDigest(journal)` (`settleSigned`) | The buyer's service produced this journal |
| `amount < proofThreshold`, `amount ≤ signerAllowance − signerSpent`, signer not revoked (`settleSigned`) | Bounds the damage of a stolen signer key |

`signerDigest(journal) = sha256("warrant/invoice-signer/v1" ‖ chainId ‖ escrow ‖
sha256(journal))`, computed identically by `evidence::signer_digest` in Rust, so
the checker needs no keccak dependency. Signatures are 65-byte `r‖s‖v`, low-s,
checked with OpenZeppelin `ECDSA.recover`. This is a service key, not a wallet
flow, so EIP-712 typed data is not used.

The rule tree, lexicon, credentials and PO lines stay inside the proven
evaluation; the contract does not interpret them.

## Evidence

- 22 Solidity tests, including fuzz cases showing cumulative payments never
  exceed the ceiling and signed payments never exceed the allowance; eleven
  deliberately removed checks are each caught. The twelfth, dropping the escrow
  address from `signerDigest`, is not observable: the journal's own `chainId` and
  `vault` fields, which the contract checks, already prevent cross-escrow replay,
  so that binding is defense in depth.
- `testRustSignatureRecoversToSigner` recovers the signer from a signature the
  Rust helper produced (`contracts/test/fixtures/invoice-signature.json`).
- `testRustJournalDecodesFieldForField` decodes a journal produced by the Rust
  checker (`contracts/test/fixtures/invoice-journal.json`); a Rust test keeps that
  fixture equal to the current encoding.
- The Arc probe (`scripts/check-arc.py`) now also constructs `InvoiceEscrow` in a
  read-only Arc Testnet simulation.
- `scripts/invoice-demo.py` runs the whole flow on a local chain: one invoice
  settled by signature, a second by real proof (`--signed-only` skips proving).

## Limits

- **Squatting.** Anyone can fund an order under a known `(policyHash, poId)` first.
  Their funds can only pay the vendor under the same policy, so this is a denial of
  service, not theft; use order numbers that are not guessable before funding.
- **Image IDs are build-specific.** Local guest builds are not byte-reproducible
  across machines. Deploy with the image ID of the exact build that will prove.
- **Unaudited prototype.** Not covered by the Verus proof, which is about the Rust
  evaluator only.
