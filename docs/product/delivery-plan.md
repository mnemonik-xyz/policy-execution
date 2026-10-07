# Delivery plan and production readiness

This is an ordered plan, not a completion claim or an estimated release date. The product scope includes all developed policy families; the compute journey remains a showcase, not a limit on site coverage. Release readiness is recorded per catalog entry and settlement adapter.

## Work packages and gates

| Phase | Deliverable | Dependency / exit evidence |
| --- | --- | --- |
| P0 — Product contract | Full policy catalog, trust/mode review, customers and commercial terms; compute showcase selection | Policy/action/settlement distinctions and complete inventory agreed |
| P1 — Live foundations | Validate each family deployment, authority integration and Circom setup provenance; merge/review pilot and run bounded io.net job | CHAIN-1–6 and CLOUD-1–2; deployment evidence per adapter; actual GPU output and cleanup |
| P2 — Durable orchestration | Store, queue/outbox, indexer, artifact service, catalog resolver and adapters for seven existing contract settlement paths | BE-1–8; restart/reconciliation drills; correct journal routing; CLOUD-3 |
| P3 — Customer application | Policy picker/editor, tasks, invoices/orders, vaults, evidence, recovery and authority/operator screens | FE-1–9 and CAT-1–7; all current settlement modes exercised through the site |
| P4 — Automated funding | CCTP quote/sign/resume and exact provider payment/credit reconciliation | FUND-1–4; capped approved live spend and restart evidence |
| P5 — Controlled launch | Security review, support ownership, observability, receipts, retention, load limits and rollback | CHAIN-5; pilot acceptance below; no unresolved duplicate-spend or leaked-resource findings |
| S1 — DeFi action extension | Verified Arc venue, swap intent schema, executor, frontend and optional proof checker | SWAP-1–5; no claim that existing payment contracts perform swaps |

Frontend construction can proceed against fixtures during P1/P2. Fixtures must be labeled and contract-tested against API schemas before switching to live data. Already-funded provider credits can support P3; automatic bridge funding must not be advertised before P4 passes. No calendar commitment is credible until account access, proving performance and live integration results are known.

Implement the shared catalog/dispatcher once, then integrate the task/vault, solver, invoice and Circom adapters with their own evidence services and manifests. The UI may expose readiness per environment while work proceeds, but “all policies supported” requires every existing adapter's acceptance evidence. Swaps are an additional track requiring new contracts; they must not block documenting or integrating existing policy paths.

## Operational requirements

| Area | Required behavior |
| --- | --- |
| Observability | Correlate workspace/deal/operation/transaction/provider IDs; redact keys, raw payment envelopes and sensitive artifacts |
| Alerts | Stuck external writes, approaching settlement deadlines, insufficient gas, proof backlog, unknown payments and unconfirmed cleanup |
| Incident handling | Named on-call owner, customer-visible status, support reference and documented reconciliation actions |
| Spend controls | Per-operation and cumulative limits; bounded credits; halt new spends on uncertainty; never imply estimate checks enforce provider billing |
| Backup and restore | Restore database, pending-operation journal, encrypted key references and artifact metadata together; demonstrate recovery |
| Deployment | Version manifests, immutable image digests, migration compatibility and old-checker support for existing agreements |
| Rollback | Disable new offers/executions independently; preserve read access, refund access and settlement support for existing accepted agreements |
| Capacity | Pilot concurrency limit fixed by measured prover throughput and supported GPU availability; explicit queue and expiry behavior |
| Privacy | Public inputs only initially; encrypted transport/storage, tenant isolation, retention notice and deletion workflow; immutable chain records explained |
| Billing | Reconcile every payout and provider payment to an authorized intent; record residual credits and actual charges |

Proposed hosted-artifact retention is seven days for pilot audio and thirty days for transcripts/evidence, configurable before launch. Financial records and legally required records need an approved retention policy for the operating entity. Never claim that deleting an account deletes chain records. Verify provider data handling before accepting confidential workloads.

## Pilot acceptance script

First run CAT-1–7: both contractor presets, supported-rule import, task and policy-vault proofs, solver proof/public result, invoice proof/signature/buyer approval, partial order closure, signer revocation, and Circom facts approval/payment/revocation. Verify incompatible journal rejection and exact boundary conditions. Use the appropriate deployment and show the actual authority model in the receipt. The compute-specific end-to-end script then follows:

1. A fresh buyer creates a supported task, reads the guarantee and approves a bounded spend. The review screen exactly matches signed/committed terms.
2. An independent seller accepts. The buyer closes the browser. The worker service computes and proves a valid schedule and settles on the intended Arc deployment.
3. Reopening the deal shows the payment, published schedule and separately authorized io.net run. The user retrieves the durable transcript and receipt. Cleanup is confirmed.
4. A deliberately invalid schedule is rejected without payout. An accepted task that misses its deadline is refunded through the actual contract path.
5. A cloud failure after successful planning settlement shows the paid fee and failed execution accurately, with the applicable service recovery action.
6. Interrupt the process around each spend boundary, including bridge and provider credit reconciliation when enabled. Resume without duplicate charge or duplicate deployment.
7. Another workspace cannot access the deal, event stream, secret or result. A viewer cannot spend. Revoked agent credentials cannot start a new operation.

Store reviewed evidence for each case. Separate replay, local/mock, testnet and mainnet evidence. A polished frontend recording alone does not pass this gate.

Run SWAP-1–5 separately before enabling swaps. Quote previews, approval transactions and successful transfer proofs do not count as actual bounded-swap execution.

## Measures that indicate a product

Measure time from draft to accepted deal, share of quotes accepted, valid proof settlement rate, proof latency/cost, workload completion rate, actual versus estimated execution charge, successful self-service recovery, support interventions per deal and repeat purchase. Track duplicate payments and unconfirmed leaked resources as release-blocking incidents. Report sample size and environment; do not populate a dashboard with invented launch metrics.

The first commercial signal is a customer returning for another task because the agreement and automation reduce their work or risk. A low-cost transcript alone does not validate demand for Warrant's planning guarantee.

## Decisions requiring evidence

| Decision | Proposed default | Resolve before |
| --- | --- | --- |
| Initial customer | Developer/compute operator hiring an independent scheduler | P0 |
| What earns the bounty | Verified feasible schedule within committed modeled cost | First signed agreement |
| Who funds execution | Designated pilot operator with separate capped authorization | Live GPU run |
| Service failure remedy and fee | Explicit separate quote; fee amount and remedy not set by escrow | Customer payment |
| Model/input and accuracy expectations | Public audio, pinned model, no correctness guarantee | Pilot task selection |
| Registry and image distribution | Operator-owned registry, digest pinning | P1 deployment |
| Arc verifier route and prover capacity | Validate actual deployed verifier/proof format and benchmark | P1/P2 |
| Customer self-service wallets | Bounded signatures; custody model requires deliberate design | Public managed-funding launch |
| Retention/support owner | Proposed retention above; assign an actual operator | P5 |
| Authority integrations | Explicit registry, reviewer, PO and invoice issuer roles; import or signing service under the correct actor | Relevant family activation |
| Circom production provenance | Reviewed circuit/setup artifacts and verifier key; no reuse of a development ceremony as a production claim | Circom real-fund activation |
| DeFi venue and authorization | Single-chain exact input; reviewed router, token/price policy and bounded intent | S1 implementation/activation |

## Review checklist for this specification

Relative links must resolve in the repository at the documented baseline. Current behavior must be distinguished from proposed service features. The UI must preserve contract deadline boundaries, separate the bounty from compute spending, and keep modeled cost separate from billing. No repo pin, implementation, live deployment, wallet or spend is changed by merging these documents.
