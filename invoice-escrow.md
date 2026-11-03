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
- **Buyer approval** (`approveInvoice`, then `settleApproved`): when the
  interpreter cannot decide (Ask), the buyer approves an amount on their own
  authority. The vendor then accepts that exact approval to get the payment. Both
  parties must agree. Same vendor, same ceiling, same deadline, same replay
  protection. The `Paid` event marks the payment as approved, not proven.

## Lifecycle

1. The buyer funds an order with `offer(Terms)`: policy hash and version, order
   number, vendor, ceiling, and the signer terms for this order: `signer`
   address, `signerAllowance` (how much can settle on signatures alone) and
   `proofThreshold` (amounts at or above it need a proof or approval). A zero
   signer means proofs and approvals only; a signer needs a nonzero allowance
   and threshold, and cannot be the vendor. The full ceiling is transferred and
   reserved.
2. The vendor calls `accept(orderId)`. From then on the buyer cannot cancel
   before `settleBy`, so the vendor can deliver against committed funds. The
   buyer alone also cannot use an approval to block the vendor's invoice. See
   [Vendor protection](#vendor-protection) for what this covers and its limits.
3. For each invoice, one of: the signing service runs `warrant-host invoice-sign`
   and anyone relays `settleSigned(journal, signature)`; a prover runs the invoice
   guest and anyone relays `settle(seal, journal)`; or, for an invoice the checker
   left undecided, the buyer calls `approveInvoice(orderId, obligationId, amount,
   documentHash)` and the vendor calls `settleApproved` with the same four values.
   The first two take the same 15-word journal and the same checks; only the
   authenticator differs.
4. After `settleBy`, anyone can `close(orderId)`; the unpaid remainder returns to
   the buyer. An unaccepted order can be closed by the buyer at any time, or by
   anyone after `acceptBy`.

`orderId = keccak256(abi.encode(chainId, escrow, customer, policyHash, poId))`.
`offer` binds `customer` to `msg.sender`; settlement reads it from the authenticated
journal. Another buyer cannot squat this namespace by copying public terms.
`consumed[customer][recipient][obligationId]` prevents replay across one buyer's
orders to one vendor address and all three settlement paths. Another buyer cannot
block the payment. The same buyer cannot block it through an order to another
address (changed 2026-11-03; before, the key was `consumed[customer][obligationId]`).

This is a breaking revision (2026-09-30): `InvoicePolicy.customer` and `InvoicePolicy.invoice_key` are required,
the policy commitment domain is `warrant/invoice-policy/v2`, and the journal
appends customer as its fifteenth word. Rebuild the invoice guest and deploy a
new escrow with its image ID. Old 14-word receipts are not compatible.

The multi-currency revision (2026-10-08) is also breaking for the guest: new PO
fields and policy domain `warrant/invoice-policy/v3` change the invoice image ID.
The contract and the journal layout are unchanged, but a deployment pins the image
ID, so the new image needs a new escrow. See [the evidence checker §4a](evidence-checker.md).

## Invoice-source authentication

The buyer includes a compressed secp256k1 public key, `invoice_key`, in the
approved policy. This key belongs to an invoice issuer or trusted intake system.
It is separate in role from the settlement signer. The source must establish
invoice provenance independently; blindly signing any agent-submitted document
would simply move the original vulnerability into that service.

`InvoiceAttestation` contains `scope` (chain, escrow and token), `customer`,
`po_id`, `document_hash` (SHA-256 of the exact XML bytes), `valid_after` and
`valid_until`. The signed message is the domain-separated typed encoding
`warrant/invoice-attestation/v1`. Reformatting XML also changes its hash.

On the authority's machine, using an independently obtained invoice and reviewed
policy/PO files:

```sh
# Construct the statement; default validity is the policy/PO intersection.
# This command checks document/PO bindings, but does not establish source truth.
target/release/warrant-evidence prepare-invoice policy.json po.json invoice.xml statement.json
# Review the statement and document before using the authority's signing key.
target/release/warrant-evidence sign-invoice invoice-authority.key statement.json signature.json
```

The agent places the statement object in `invoice_attestation` and the JSON byte
array from `signature.json` in `invoice_signature` in its `SignRequest`. Neither
command overwrites an existing output. The signing utility is an issuer tool,
not an automatic service exposed to untrusted agents.

The checker requires this evidence before evaluating any rule, including `Any`.
If both fields are absent it returns `Ask(InvoiceAttestationMissing)`. A partial
pair, invalid signature, changed document, wrong customer, wrong PO or wrong
payment scope rejects the request. Its validity interval constrains the journal.
The evidence commitment includes the attestation and signature; checker version
2 identifies these semantics. The contract's signed path still trusts its
settlement signer to run this checker; a compromised signer is bounded by its
on-chain allowance. The proof path verifies execution of the pinned checker.

A source signature establishes endorsement of bytes, not delivery, price
fairness or unique business debt. Buyer approval can override the checker on
the buyer's authority; it does not claim policy compliance.

## The signing service

`warrant-host invoice-sign key.hex policy.json rpc-url request.json output.json`
is the buyer-run service behind the signature path. It is built so that the
untrusted agent cannot feed it anything the checker does not re-verify:

- **What the agent sends** is a `SignRequest`: the invoice bytes, its line
  claims, the vendor credential and purchase order with their signatures, and the
  invoice-source attestation and signature, plus optional reviewer acceptance. The type rejects unknown fields, so a request that
  carries a `policy` or a `po_spent` value fails to parse.
- **What the service holds** is the policy file and the signing key.
- **What the service reads from the escrow**, over JSON-RPC at signing time, is
  the order: its customer, state, policy hash and version, recipient, signer, spend, signer
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
        Chain->>Chain: journal and customer checks, threshold, allowance, ECDSA.recover == order.signer
        Chain-->>Vendor: USDC
    else Allow, but a proof is needed
        Service-->>Agent: refused: amount at or above threshold or over allowance
    else Ask
        Service-->>Agent: no signature, ask record (reasons, obligationId, documentHash, payable)
    else Deny or invalid
        Service-->>Agent: error, nothing written
    end
```

The service checks the customer, policy, signer and selected live bounds before
signing. These reads are not an atomic reservation or a settlement guarantee:
state can change, and the contract rechecks authorization at settlement. The `spent`
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
    Buyer->>Buyer: reads the invoice, decides
    Buyer->>Chain: approveInvoice(orderId, obligationId, payable, documentHash)
    Chain->>Chain: accepted order, before settleBy, msg.sender is the buyer, obligation unused
    Note over Chain: InvoiceApproved — records the approval, pays nothing, consumes nothing
    Vendor->>Chain: settleApproved(orderId, obligationId, payable, documentHash)
    Chain->>Chain: msg.sender is the vendor, values equal the approval, within ceiling, obligation unused
    Chain-->>Vendor: USDC
    Note over Chain: Paid(..., BuyerApproval, ...) — a later proof or signature for the same obligation reverts
```

Approval is bounded like every other path: it pays only the order's vendor, only
within the remaining ceiling, only before the deadline, and consumes the
obligation ID across all paths and all orders of the same customer to that vendor.
The contract does not require evidence of a previous `Ask`; approval is an
explicit buyer override, including for invoices the checker would deny. It is not
bounded by the signer allowance or the threshold, because the buyer and the vendor
both agree to it with their own keys.

The approval is a record on the chain, not a signature. It is valid only for its
order and only for the order's vendor. A new approval for the same order and
obligation replaces the previous one. The vendor can wait: until the vendor calls
`settleApproved`, a proof or a signature can still pay the obligation in full.

## Vendor protection

The invoice escrow protects the vendor against a buyer who does not cooperate after
`accept`. The buyer cannot cancel the order before `settleBy`, and anyone can relay
a proof with `settle`. The buyer alone cannot consume the vendor's obligation ID.

Before 2026-11-03, the buyer alone could call `settleApproved`. A payment of one
unit consumed the obligation ID. The vendor's valid proof for the same invoice then
reverted with `AlreadyPaid`, and after `settleBy` the buyer got the remainder back
with `close`. This was reproduced on 2026-11-03 with the test verifier: a one-unit
approval blocked a 1,000-unit proof. A buyer could also open a second order to an
address that they control and consume the obligation ID there, because the
`consumed` key had no recipient.

Now (verified 2026-11-03 with Foundry tests, test verifier only):

- `approveInvoice` records the buyer's approval only. It pays nothing and consumes
  nothing (`testCustomerCannotPreemptProofWithOneUnitApproval`).
- Only the order's vendor can call `settleApproved`, and only with the amount and
  document hash that the buyer approved. Another party, another order or other
  values revert (`testCopiedApprovalByThirdPartyReverts`).
- The vendor can accept a reduced amount. This closes the obligation one time, on
  every path (`testVendorAcceptedReducedAmountClosesOnce`).
- An order to another recipient cannot consume this vendor's obligation ID
  (`testSideOrderToOwnAddressCannotConsumeVendorObligation`).

What this does not protect:

- **The signer path.** On an order with a signer, the vendor trusts that signer.
  The buyer runs the signing service. That signer can sign a small journal for the
  vendor's obligation ID and consume it, below `proofThreshold` and within
  `signerAllowance` (`testOrderSignerCanStillConsumeWithinItsAllowance`). A vendor
  who does not trust the signer must accept only a proofs-only order (zero signer).
- **Access to a proof.** The buyer writes the policy and names its authorities,
  for example `invoice_key`. If the buyer controls an authority that the vendor
  needs, or the policy does not allow the vendor's invoices, the vendor cannot get a
  proof. The vendor must review the policy and its authorities before `accept`.
- **Orders that the vendor accepted.** The buyer can open another order to the same
  vendor address with a policy and a signer that the buyer controls. If the vendor
  accepts that order, it shares the vendor's obligation IDs. The vendor must accept
  an order only if it accepts the order's policy and signer.
- **Time.** The vendor must settle before `settleBy`. After it, `close` returns the
  unpaid remainder to the buyer.

## What settlement enforces

| Check | Why |
| --- | --- |
| Journal is exactly 15 words: the 12 `Authorization` words, `poId`, `poMaxTotal`, `customer` | A task-escrow journal cannot be replayed here |
| Order found by `(customer, policyHash, poId)` is Accepted and `settleBy` has not passed | Only committed orders pay |
| `policyVersion`, chain, escrow address, token match | Domain binding |
| `recipient` equals the order's vendor | The invoice cannot redirect payment |
| `poMaxTotal` equals the funded ceiling | The proven order is the funded order |
| `amount ≤ maxTotal − spent` (live state) | The checker's `po_spent` input can be stale; the contract's is not |
| Obligation ID not yet consumed, across the **same customer's** orders to the **same vendor address** and all paths | An invoice pays once, even under another order or by another route; an order to another address cannot block it |
| Nonzero amount, obligation, deliverable and evidence hashes; journal validity window | Well-formed authorization |
| Proof verifies for the pinned invoice image ID (`settle`) | The fixed interpreter produced this journal |
| Signature recovers to the order's `signer` over `signerDigest(journal)` (`settleSigned`) | The buyer's own service produced this journal |
| `amount < proofThreshold`, `amount ≤ signerAllowance − signerSpent`, signer not revoked (`settleSigned`) | Bounds the damage of a stolen signer key to this order's terms |
| `msg.sender` is the order's customer, amount, obligation and document hash nonzero (`approveInvoice`) | Only the buyer approves, and the record is complete |
| `msg.sender` is the order's vendor, and amount and document hash equal the buyer's approval for this order and obligation (`settleApproved`) | The buyer alone cannot consume an obligation; a copy of the approval pays nobody else |

`signerDigest(journal) = sha256("warrant/invoice-signer/v1" ‖ chainId ‖ escrow ‖
sha256(journal))`, computed identically by `evidence::signer_digest` in Rust, so
the checker needs no keccak dependency. Signatures are 65-byte `r‖s‖v`, low-s,
checked with OpenZeppelin `ECDSA.recover`. This is a service key, not a wallet
flow, so EIP-712 typed data is not used.

The rule tree, lexicon, credentials and PO lines stay inside the proven
evaluation; the contract does not interpret them.

## Evidence

- 35 invoice Solidity tests (verified 2026-11-03, Foundry v1.8.4), including
  customer-isolation and legacy-journal regressions, including fuzz cases showing
  cumulative payments never exceed the ceiling and signed payments never exceed the
  allowance; a signer named on one order signs nothing for another; buyer approval
  needs the vendor's acceptance and respects the ceiling, the deadline and the
  shared obligation registry; inconsistent signer terms are refused at `offer`.
  The [vendor protection](#vendor-protection) regressions are listed in that section.
- `testRustSignatureRecoversToSigner` recovers the signer from a signature the
  Rust helper produced (`contracts/test/fixtures/invoice-signature.json`).
- `testRustJournalDecodesFieldForField` decodes a journal produced by the Rust
  checker (`contracts/test/fixtures/invoice-journal.json`); a Rust test keeps that
  fixture equal to the current encoding.
- Host unit tests pin `orderIdFor`'s layout, the signer address derivation and
  the `order()` decoding the service relies on.
- The Arc probe (`scripts/check-arc.py`, now in [warrant-canary-deployment](https://github.com/mnemonik-xyz/warrant-canary-deployment)) constructs `InvoiceEscrow` in a
  read-only Arc Testnet simulation.
- `scripts/invoice-demo.py` runs the whole flow on a local chain: one invoice
  settled by signature (the service reading the order from the chain), an
  undecided one by buyer approval and vendor acceptance, and a third by real proof
  (`--signed-only` skips proving). The approval step changed on 2026-11-03 and is
  not yet run end to end (unverified). It also shows the service refusing a request that smuggles
  `po_spent` and a key the order does not name.

## Limits

- **Invoice identity.** The obligation ID binds a seller and invoice number, not
  a unique real-world debt. An agent cannot change that number without a new
  invoice-authority signature. The authority can still endorse a reissued or
  false invoice; source honesty and business-level deduplication remain trusted.
- **Signer compromise and availability.** A compromised settlement signer can
  submit arbitrary obligation IDs within an order it can authorize. Consuming
  an ID also blocks it on the same customer's other orders to the same vendor.
  Allowances bound token payments, not that availability impact. Other customers
  and other vendor addresses stay isolated. The buyer runs the signer, so the
  vendor trusts the buyer on this path. See [Vendor protection](#vendor-protection).
- **Scope of replay protection.** Consumption is per funding address and per vendor
  address, not global across a company's wallets, deployments or chains. If a
  vendor registry credentials two addresses for one seller tax ID, and the buyer
  funds an order to each address, the same invoice can pay one time to each
  address. Registry honesty covers this case. Buyer approval must supply the
  correct obligation ID; the contract cannot derive it from a document hash.
- **Image IDs.** Local guest builds differ across machines. Build with
  `RISC0_USE_DOCKER=1` (see the README) for a reproducible image ID, and deploy
  with that.
- **Unaudited prototype.** Not covered by the Verus proof, which is about the Rust
  evaluator only.
