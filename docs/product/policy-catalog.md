# Complete policy catalog and settlement routing

Scope decision, 2026-10-04: the site must expose **all currently developed Warrant policy paths**, not only the io.net showcase. This document supersedes the earlier compute-only launch scope. UI integration and verified deployments are still required; implemented contracts are not evidence that website settlement is live.

## Catalog inventory

A policy defines authorization conditions. A funding arrangement defines who retains control of money. A settlement mode defines the authenticator the contract accepts. These are separate choices with an explicit compatibility matrix.

| Site entry | Existing implementation | Customer configuration and evidence | Settlement adapter |
| --- | --- | --- | --- |
| Approved contractor | `templates/accepted-contractor-v1.json` | Amount cap, allowed vendor categories, registry and reviewer keys; signed credential and task acceptance | Task proof, or explicitly selected revocable policy vault |
| Specific accepted deliverable | `templates/accepted-deliverable-v1.json` | Contractor conditions plus fixed recipient and expected deliverable hash | Task proof, or explicitly selected revocable policy vault |
| Verified compute schedule | `core/src/solver.rs`, solver guest | Committed instance, cost ceiling, scheduling constraints, scope and policy rule; real receipt and canonical schedule bytes | Solver proof with result publication |
| Invoice against purchase order | `core/src/evidence.rs`, invoice guest | Customer, vendor credential, signed PO, invoice authority, exact UBL bytes, checked line evidence, approved lexicon, rules; optional reviewer | Invoice proof, authorized signer, or explicit buyer approval |
| Policy-controlled spending vault | `PolicyExecutionVault.sol`, task guest | Supported task-policy rule tree and authority keys; lifetime budget, hard payment cap and owner controls | Vault proof |
| Fixed-circuit payment | `proof-execution`, `ProofPolicyVault.sol` | One allowed category, amount cap, validity, policy version; owner-approved facts commitment | Circom/Groth16 proof |
| Bounded DeFi swap | New specification only | Token pair, exact input, output minimum, recipient, route, expiry and spend authorization | New swap executor; see [swap specification](defi-swaps.md) |

The fixed-circuit entry remains visible and integrable through the same site, with its separate setup/provenance requirements. Its current single-machine development setup cannot be presented as production-ready. Do not silently migrate its commitments to RISC Zero or label it as a task escrow.

“Specific accepted deliverable” requires the expected hash before commitment. When the result is not yet known, use the approved-contractor path: the reviewer's acceptance later binds the actual result hash. That signature is an authority judgment, not a proof of objective artifact quality.

## Every implemented rule remains available

The site provides presets plus a schema-checked advanced editor/importer for the full existing typed language. It does not restrict users to preset combinations or accept natural-language rules as executable authorization.

| Rule | Task / policy vault | Solver | Invoice |
| --- | --- | --- | --- |
| `all`, `any` | Yes | Yes | Yes; three-valued evaluation |
| `amount_at_most` | Payment cap | Planning reward cap, distinct from schedule cost | Invoice payment cap |
| `vendor_category_in` | Registry-authenticated category | Unsupported | Registry-authenticated category |
| `accepted` | Designated reviewer's signed acceptance | Checker establishes schedule acceptance | Optional signed reviewer evidence; missing facts may cause Ask |
| `deliverable_equals` | Exact artifact hash | Canonical schedule hash | Exact invoice-byte hash |
| `recipient_equals` | Payment recipient | Solver recipient | Credential/order recipient |
| `line_labels_within` | Unsupported | Unsupported | Checked PO-line or lexicon-span evidence |
| `no_denied_term` | Unsupported | Unsupported | Literal configured term scan, not semantic classification |
| `within_po` | Unsupported | Unsupported | PO and policy limits with live spend rechecked at settlement |

Mandatory authentication and family-specific gates run before the rule tree. `any` cannot bypass a required signature or the solver checker. Conversely, the site must not imply that an optional predicate is enforced when the chosen rule omits it or permits another branch. Explain the concrete rule, authorities and result of evaluation before approval. Preserve existing depth/node/collection limits and typed validation.

The Circom circuit is a separate fixed language: one nonzero category, positive amount no greater than its cap, and an ordered validity interval, plus contract checks and owner-approved facts. The Rust rule editor is unavailable for that entry.

## Adapter matrix

These adapter IDs are proposed site integration identifiers for existing contract entry points; the hosted adapters have not been implemented yet.

| Adapter ID | Contract entry point | Proof / authorization format | Lifecycle and trust |
| --- | --- | --- | --- |
| `task-proof-v1` | `TaskEscrow.settle(seal,journal)` | Task guest, 12 words / 384 bytes | Fixed funded amount and recipient; seller accepts; payout or timeout refund |
| `solver-proof-v1` | `SolverBountyEscrow.settleWithResult(seal,journal,result)` | Solver guest, 13 words / 416 bytes | Task lifecycle; public result mandatory; hash-only path rejected |
| `invoice-proof-v2` | `InvoiceEscrow.settle(seal,journal)` | Invoice guest, 15 words / 480 bytes | Multiple invoices per accepted PO; fixed vendor and aggregate ceiling |
| `invoice-signature-v2` | `InvoiceEscrow.settleSigned(journal,signature)` | Same 15-word journal; approved signer digest | Buyer-designated service trusted; amount strictly below threshold and within remaining signer allowance |
| `invoice-approval-v2` | `InvoiceEscrow.settleApproved(orderId,obligationId,amount,documentHash)` | Buyer's direct transaction; no policy proof | Explicit override, including a Deny; still bounded by vendor, ceiling, deadline and replay checks |
| `vault-proof-v1` | `PolicyExecutionVault.pay(seal,journal)` | Task guest, 12 words / 384 bytes | Owner can pause, withdraw, cancel and rotate policy; no irrevocable task reservation |
| `circom-payment-v1` | `ProofPolicyVault.pay(a,b,c,publicSignals)` | Fixed circuit, 11 public signals and pinned verifier key | Owner approves facts; revocable vault; separate commitments/setup |

The API and UI record the actual authenticator on every payment: “Proof verified”, “Authorized signer”, or “Buyer approved”. Never call the latter two proof-based settlement. A proof on the task path still trusts the named evidence authorities.

Invoice `Paid` is a child payment event, not a terminal order state. The order remains accepted and can pay more invoices until its ceiling or deadline, then `close` returns its unspent remainder. Closing an accepted order is allowed only strictly after `settleBy`. Signer revocation stops signature settlement but leaves valid proof and buyer-approval paths available. The buyer can override without an on-chain Ask record; the site requires a separate deliberate approval with a visible rationale and never silently falls back to it.

Task and solver escrows retain their documented acceptance/refund boundaries. Vaults have no seller acceptance, guaranteed future payout, or timeout refund; show owner withdrawal and revocation controls instead. Updating a vault policy advances its version and can invalidate pending proofs. Deposits, withdrawals and policy rotation do not reset lifetime spend.

## Common site architecture

The reusable shell is **Select policy → configure → review authority and funds → submit evidence/action → track outcome and recovery**. Adapters add the required steps: seller acceptance for escrows, child invoices for orders, policy/facts approval for vaults and quotes for swaps. “One flow” means shared infrastructure, not forcing distinct contract states into a single state machine.

Catalog metadata includes `templateId`, content digest/version, family, parameter/evidence schemas, supported rule atoms, allowed adapters, public-data notice and source provenance. An independent deployment manifest includes environment, chain, contract/token addresses and decimals, guest image or verifier key, ABI/schema version and readiness evidence. A caller cannot select an arbitrary contract or adapter by changing an ID.

The backend resolves a compatible deployment before commitment, canonicalizes using the original Rust or Circom libraries and presents the concrete policy hash and signing summary. The seller or owner independently checks the same commitment. Pin the manifest and template digest to each operation; catalog updates do not mutate existing agreements. Missing or incompatible deployments yield a specific setup status, never a pretend success or substitution of authenticator.

## Site acceptance criteria

- CAT-1: Both existing template files, the solver path, invoice family, policy vault and Circom entry are discoverable and configurable from the website; advanced imports cover every supported rule.
- CAT-2: Each of the seven proposed adapter IDs above, covering existing contract settlement paths, has a browser-driven successful settlement test on a compatible deployment, plus a rejected invalid/replayed authorization. Local/testnet/mainnet evidence is labeled separately.
- CAT-3: Wrong guest, journal size, contract, template family or environment is rejected before signing and by the underlying contract where applicable.
- CAT-4: Invoice threshold equality requires proof or explicit buyer approval; exhausted/revoked signer allowance cannot trigger silent fallback. One obligation cannot pay again through another mode.
- CAT-5: Partially paid orders display remaining funds and close/refund correctly. Revocable vaults never display a false seller payment guarantee.
- CAT-6: Circom facts approval, revocation and proof verification use its own commitments, verifier key and single-use obligation IDs; production enablement requires reviewed ceremony artifacts and deployment evidence.
- CAT-7: The release coverage report enumerates every catalog entry and settlement mode. An unfinished family is visible as unfinished; the site is not described as supporting all policies until CAT-1–6 pass.

## Source inventory

Inspected 2026-10-04: [templates](../../templates/README.md), [task authorization](../../core/src/lib.rs), [solver](../../core/src/solver.rs), [invoice checker](../../core/src/evidence.rs), [rule enum](../../verified/src/lib.rs), [task escrow](../../contracts/src/TaskEscrow.sol), [solver escrow](../../contracts/src/SolverBountyEscrow.sol), [invoice escrow](../../contracts/src/InvoiceEscrow.sol), [policy vault](../../contracts/src/PolicyExecutionVault.sol), [Circom prototype](https://github.com/mnemonik-xyz/proof-execution/blob/main/README.md) and [Circom vault](https://github.com/mnemonik-xyz/proof-execution/blob/main/contracts/ProofPolicyVault.sol). “Developed” refers to these executable paths, not unimplemented ideas in research documents.
