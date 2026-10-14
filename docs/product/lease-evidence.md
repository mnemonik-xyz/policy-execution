# Specification: the metered lease policy family

Status: **proposed, 2026-10-14. Nothing in this document is implemented.**
The metered lease is a Warrant policy family, beside task, invoice and solver. It is
part of production readiness: track L1 in the [delivery plan](delivery-plan.md) and
an entry in the [policy catalog](policy-catalog.md). The compute marketplace in
[warrant#13](https://github.com/mnemonik-xyz/warrant/pull/13) is its first consumer,
not its owner.

Abbreviations: GPU — Graphics Processing Unit.

## 1. Goal

Add a lease guest, so that a metered service settles through `InvoiceEscrow` without
a contract change. The buyer funds one order with the ceiling. The seller settles each
interval with a proof that a buyer-signed voucher for that interval exists. A GPU lease
is the first service. Any service billed per interval against a buyer voucher fits.

Catalog entry: **Metered lease**. Adapters: `lease-proof-v1` on `InvoiceEscrow.settle`
and `lease-signature-v1` on `InvoiceEscrow.settleSigned`, both with the fifteen-word
journal and a deployment pinned to the lease image ID.

## 2. The module

The module `core/src/lease.rs` follows the solver path: mandatory gates first, the rule tree last. It
reuses `Acceptance` as the voucher, `VendorCredential` as the seller credential, the
existing `verify`, and the fifteen-word journal layout of the invoice path. Nothing
changes in Solidity.

```rust
// core/src/lease.rs
pub const CHECKER_VERSION: u32 = 1;

pub struct LeasePolicy {
    pub version: u64,
    pub customer: Address,        // the funding buyer, bound into the journal
    pub scope: Scope,             // chain, escrow, token
    pub valid_after: u64,
    pub valid_until: u64,
    pub registry_key: Vec<u8>,    // vouches for the seller; the buyer's own key today
    pub acceptance_key: Vec<u8>,  // signs the interval vouchers
    pub checker_version: u32,
    pub lease_id: Hash,           // poId on chain
    pub rate: u64,                // USDC atomic units per interval
    pub max_intervals: u32,       // ceiling = rate * max_intervals = poMaxTotal
    pub rule: Rule,
}

pub struct LeaseInput {
    pub policy: LeasePolicy,
    pub request: Request,         // task_id = obligation, deliverable_hash = token hash
    pub interval: u32,
    pub vendor: VendorCredential,
    pub vendor_signature: Vec<u8>,
    pub acceptance: Acceptance,   // the voucher
    pub acceptance_signature: Vec<u8>,
}

pub struct LeaseAuthorization {
    pub authorization: Authorization,
    pub lease_id: Hash,
    pub ceiling: u64,
    pub customer: Address,
}
// journal(): the 12 base words, then lease_id, ceiling, customer,
// byte for byte the InvoiceAuthorization layout.

pub fn obligation_id(lease_id: &Hash, interval: u32) -> Hash {
    hash_tagged(b"warrant/lease-obligation/v1", &(lease_id, interval))
}

pub fn authorize_lease(input: &LeaseInput) -> Result<LeaseAuthorization, LeaseError>
```

`authorize_lease` checks, in this order:

1. Policy: version, scope, both keys, checker version, rate and maximum intervals nonzero, no overflow in the ceiling, rule valid with the invoice flag off.
2. Request: scope, amount equals the rate, recipient nonzero, `task_id` equals `obligation_id(lease_id, interval)`, interval below the maximum.
3. Credential: scope, recipient equals the request recipient, signature under the registry key.
4. Voucher: scope, `task_id`, `deliverable_hash`, recipient and amount equal the request, `accepted` true, signature under the acceptance key.
5. Validity window: the intersection of the policy, credential and voucher windows.
6. Facts: amount, category, accepted, deliverable, recipient. Then `evaluate`.
7. Evidence hash over the checker version, the credential and the voucher.

The module does not know the spent ceiling. The contract checks that. It does not
know whether interval k minus one was paid. Each interval is its own obligation. It
does not know whether the GPU ran. Nobody can check that.

The rest of the delta:

| Piece | Work |
|---|---|
| `methods/lease-guest` | Read `LeaseInput`, call `authorize_lease`, commit the journal. Add the name to `build.rs`. |
| Host | `lease-image-id`, `lease-evaluate`, `lease-execute`, `lease-prove`, copied from the solver ones. |
| Signer path | `signer_digest` and `sign_journal` from `evidence.rs` apply unchanged. |
| Fixtures | One lease journal for the Solidity test that mutates all fifteen words. |
| Template | `templates/lease-v1.json` with rate, interval and ceiling as parameters. |
| Deployment | `DeployInvoice.s.sol` with the lease image ID. |
| Tests | The positive path, every gate, removed-check mutations. |

About one engineer-week. The guardrail needs no new signing tool. `warrant_sign_acceptance`
with a derived `task_id` is the voucher. The Verus proof covers the evaluator only. The
lease checker is tested, not proved, like the other two checkers.

## 3. What the seller's proof says, and what a cheating buyer can do

The proof the seller presents at settlement is a RISC Zero receipt for the lease
image, or the buyer's countersignature on the same journal below the threshold. It
establishes one statement: under policy hash P, a voucher signed by the buyer's
acceptance key exists for lease L and interval k, with token hash T, recipient R and
amount equal to the rate, and the seller holds a valid credential. The contract then
checks the order, the ceiling, the deadline, and that interval k was not paid before.

The proof says nothing about the GPU. The evidence of service is the buyer's signature,
which the buyer gives after the interval was served. The seller's defence is the
interval token: no voucher, no next token.

What a buyer can try, and what happens:

| Attempt | Result |
|---|---|
| Use interval k, refuse to sign | No token for k plus one. The seller loses one interval. The buyer loses the rest of the lease and keeps the ceiling locked until `settleBy`. |
| Sign, then try to take the money back | Impossible. After `accept` the buyer cannot close before `settleBy`. A consumed obligation stays paid. |
| Sign a voucher with a wrong amount, recipient or token | The checker rejects it. Same as a refusal. |
| Sign a voucher with a window too short to settle | The seller checks the window before it releases the next token. A voucher the seller cannot settle counts as a refusal. |
| Revoke the signer mid-lease | `revokeSigner` only tightens. The seller settles the same voucher through the proof path. |
| Issue a seller credential that expires mid-lease | The seller checks at `accept` that the credential window covers the whole lease plus a settlement margin. |
| Pin a rule that no voucher can satisfy | The seller inspects the policy before `accept`, as the showcase already does when it verifies the policy hash. |
| Fund less than the ceiling | Impossible. `offer` reserves the whole ceiling. |
| Redirect the payment | Impossible. The recipient is fixed at `offer`. |
| Refuse to sign interval zero after provisioning | The seller loses the provisioning minutes. This is the no-show residue. |

The seller's rules before `accept` are therefore part of the design: the policy names
the agreed keys and rate, the rule passes for a sample voucher, and the credential and
policy windows cover the lease. The seller's rules during the lease are two: release a
token only against a settleable voucher, and settle each voucher inside its window.

The seller can cheat symmetrically in one way: take the token fee for interval k and
stop. The buyer then signs nothing more and loses nothing beyond interval k, because
it signs after service. A seller cannot settle without a voucher, and cannot settle
early, because the guardrail sets each voucher's `valid_after` to the end of its
interval and the contract enforces the window.

## 4. Acceptance criteria

- LE-1: `authorize_lease` rejects every gate of section 2 in a dedicated test, and a removed-check experiment fails for each.
- LE-2: The lease journal is byte for byte the `InvoiceAuthorization` layout, and the Solidity fixture test that mutates all fifteen words passes with a lease journal.
- LE-3: `settleSigned` and `settle` on `InvoiceEscrow` both pay one lease interval on local Anvil, and a second settlement of the same interval is rejected.
- LE-4: A voucher with `valid_after` in the future is rejected by the contract until that time.
- LE-5: `lease-image-id` is recorded from a `RISC0_USE_DOCKER=1` build before any deployment.

## 5. Effort

About one engineer-week: the module and its tests, the guest, four host subcommands,
one fixture, one template, one deployment script run. The guardrail needs no new
signing tool. The Verus proof covers the evaluator only. The lease checker is tested,
not proved, like the other two checkers.
