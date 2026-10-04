# Delivery plan and production readiness

This is an ordered plan, not a completion claim or an estimated release date. One useful customer journey is the release unit.

## Work packages and gates

| Phase | Deliverable | Dependency / exit evidence |
| --- | --- | --- |
| P0 — Product contract | Pilot customer, useful public workload, commercial terms, prototype review | Confirm that buying a verified plan plus separately managed execution solves a real need |
| P1 — Live foundations | Merge/review pilot; build pinned worker; validate Arc verifier/escrow; run bounded io.net job | CHAIN-1–4 and CLOUD-1–2; recorded addresses, proof, actual GPU output and confirmed cleanup |
| P2 — Durable orchestration | Store, queue/outbox, indexer, artifact service, signer policies, seller adapter | BE-1–6; restart and reconciliation drills; CLOUD-3 |
| P3 — Customer application | Create/review/fund, deal detail, result, evidence, recovery; seller/operator screens | FE-1–7; a user completes the workflow without CLI help |
| P4 — Automated funding | CCTP quote/sign/resume and exact provider payment/credit reconciliation | FUND-1–4; capped approved live spend and restart evidence |
| P5 — Controlled launch | Security review, support ownership, observability, receipts, retention, load limits and rollback | CHAIN-5; pilot acceptance below; no unresolved duplicate-spend or leaked-resource findings |

Frontend construction can proceed against fixtures during P1/P2. Fixtures must be labeled and contract-tested against API schemas before switching to live data. Already-funded provider credits can support P3; automatic bridge funding must not be advertised before P4 passes. No calendar commitment is credible until account access, proving performance and live integration results are known.

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

1. A fresh buyer creates a supported task, reads the guarantee and approves a bounded spend. The review screen exactly matches signed/committed terms.
2. An independent seller accepts. The buyer closes the browser. The worker service computes and proves a valid schedule and settles on the intended Arc deployment.
3. Reopening the deal shows the payment, published schedule and separately authorized io.net run. The user retrieves the durable transcript and receipt. Cleanup is confirmed.
4. A deliberately invalid schedule is rejected without payout. An accepted task that misses its deadline is refunded through the actual contract path.
5. A cloud failure after successful planning settlement shows the paid fee and failed execution accurately, with the applicable service recovery action.
6. Interrupt the process around each spend boundary, including bridge and provider credit reconciliation when enabled. Resume without duplicate charge or duplicate deployment.
7. Another workspace cannot access the deal, event stream, secret or result. A viewer cannot spend. Revoked agent credentials cannot start a new operation.

Store reviewed evidence for each case. Separate replay, local/mock, testnet and mainnet evidence. A polished frontend recording alone does not pass this gate.

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

## Review checklist for this specification

Relative links must resolve in the repository at the documented baseline. Current behavior must be distinguished from proposed service features. The UI must preserve contract deadline boundaries, separate the bounty from compute spending, and keep modeled cost separate from billing. No repo pin, implementation, live deployment, wallet or spend is changed by merging these documents.
