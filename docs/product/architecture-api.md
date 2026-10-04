# Backend, API and agents

Proposed implementation contract. These HTTP endpoints do not exist in the current pilot.

The backend supports every entry and authenticator in the [policy catalog](policy-catalog.md). The original solver-specific endpoints below remain one family adapter; they must not be reused with incompatible journals or assumed to cover invoice orders and revocable vaults.

## Components and responsibilities

| Component | Owns | Must not claim |
| --- | --- | --- |
| Web/API service | Identity, workspace, drafts, authorization, signed-intent review, artifact access | That a transaction succeeded from client reports |
| Workflow workers | Durable steps, leases, retries, timeouts, reconciliation | Exactly-once side effects from an HTTP timeout |
| Chain indexer | Logs, receipts, block identity, contract reads, reorg reconciliation | Finality from submission alone |
| Seller adapter / prover | Quote, acceptance, schedule, checker, real receipt generation | Cloud execution correctness |
| io.net runner | Quote, provision, workload, collect, terminate | A provider estimate is a hard billing cap |
| Funding service | Transfer intents, wallet policy, CCTP progress, provider payment | An atomic cross-chain purchase |
| Artifact store | Immutable input/output/evidence objects, digests, retention | That a hash proves off-chain facts |
| Catalog / deployment resolver | Template digest, family schemas, supported rules, compatible ABI and image/verifier manifest | That changing an ID authorizes a different contract or trust mode |
| Settlement adapters | Task, solver, invoice proof/signature/approval, policy vault and Circom routes | A single journal or state machine works for all families |

Use PostgreSQL as the authoritative application store, an outbox and durable queue for work, and private object storage for artifacts. At-least-once delivery is assumed. Claim operations with database leases and uniqueness constraints; reconcile expired leases before repeating an external write. The contract remains authoritative for escrow.

## Domain model

| Entity | Required fields / invariants |
| --- | --- |
| Workspace / membership | Stable ID, role, account status; every private query scoped by workspace |
| Agent | Workspace/curated identity, payout wallet, checker versions, signing policy reference, health timestamp |
| Workload | Template version, input digest, size, media type, artifact reference, immutable model/image references, resource mapping |
| Deal | ID, workspace, revision, environment, template, buyer, seller, instance/policy refs and hashes, reward, deadlines, task ID and chain scope |
| Quote | ID, issuer, exact deal revision/hash, expiry, model cost, provider estimate reference, fee breakdown, signature/authentication evidence |
| Execution authorization | Deal ID, actor, budget/currency, resource limits, quote digest, expiry, allowed provider/network, retries allowed, wallet/account reference |
| Operation | Kind, immutable intent hash, idempotency key, status, attempts, external IDs, timestamps, last observed error, reconciliation record |
| Artifact | SHA-256, byte length, type, provenance, visibility, retention expiry, storage reference |
| Event | Workspace, deal, monotonically ordered cursor, type, timestamp, source, operation ID, payload version |
| Policy configuration | Family, template digest/version, canonical bytes/hash, rule tree or circuit fields, approved authorities, compatible adapter |
| Purchase order / invoice | Order terms and confirmed spend; each child invoice's exact bytes/hash, obligation ID, evidence, decision and actual authenticator |
| Vault / payment request | Owner, confirmed policy/version, token balance, lifetime budget/spend, pause/revocation and per-request evidence/consumption |
| Deployment manifest | Chain, contract/token, ABI, guest image or verifier-key hash, source/build provenance and readiness evidence |

Amounts, cost values, block numbers and all unsigned 64-bit values use decimal strings in the HTTP API. Never parse them as JavaScript Number. Addresses/hashes have schema-validated lengths. UI display decimals do not change atomic amounts. Exact canonical commitment bytes are produced by the pinned Rust implementation and preserved; arbitrary JSON reserialization in the browser is not a hashing specification.

Example money object: `{"asset":"USDC","chainId":"5042","token":"0x3600000000000000000000000000000000000000","decimals":6,"atomic":"2000000"}`. Model cost has an explicit `unit` and is not implicitly USDC.

## State ownership

`escrowState = missing | offered | accepted | paid | refunded` mirrors the contract. Draft is an application state before an offer. Funding/acceptance/settlement/refund transactions have their own `operationState = prepared | awaiting_signature | submitted | confirming | succeeded | failed | outcome_unknown`.

That escrow enum applies to task/solver adapters only. Invoice `orderState = missing | offered | accepted | closed` coexists with per-invoice payment state, `decision = allow | ask | deny | invalid` and `authenticator = proof | signature | buyer_approval`. An order can remain accepted after its entire ceiling is spent; do not invent a contract state. Vault state includes policy/version, paused flag, budget/spend and cancellation/facts-revocation mappings; no escrow acceptance/refund. Swap state is separate in [defi-swaps.md](defi-swaps.md).

`proofState = not_started | computing | proving | ready | rejected | published` and `executionState = not_authorized | queued | provisioning | running | collecting | completed | failed | interrupted` are independent. Cleanup is `not_needed | active | requested | outcome_unknown | confirmed`. Bridge/payment states are specified in [execution and funding](execution-funding.md).

Allow only enumerated transitions and record every change. A failed proof does not close an accepted escrow; a seller can retry within unchanged terms and deadline. Completion means durable result retrieval, not merely a process exit. Corrections to a chain observation append compensating events; do not erase audit history.

## HTTP contract

All routes below are under `/v1`. Mutations require authentication, workspace authorization, validated input and an `Idempotency-Key`. The server binds the key to actor, route and canonical request digest. Reuse with changed input returns 409. Same input returns the original resource/operation. Preserve deduplication records for the lifetime of any financial operation.

| Method and route | Request | Response / behavior |
| --- | --- | --- |
| `POST /workloads/uploads` | Name, size, media type, SHA-256 | Restricted short-lived upload grant; checksum validation required before use |
| `POST /deals` | Template, workload ID, typed instance, seller, reward, deadlines | 201 draft with revision, validated constraints and visibility notice |
| `PATCH /deals/{id}` | Editable draft fields + `If-Match` revision | New revision; invalidate prior quotes; 409 once offered |
| `POST /deals/{id}/quotes` | Exact revision and requested seller | 202 quote operation; no spend |
| `GET /deals/{id}/quotes` | Pagination cursor | Actual offers with expiry, digest and cost breakdown |
| `POST /deals/{id}/offer-intents` | Quote ID, quote digest, deal revision | Transaction intents for required allowance and offer; fully decoded signing summary |
| `POST /operations/{id}/transaction` | Signed transaction hash and network | 202 observation request; verify sender, chain, calldata and receipt independently |
| `POST /deals/{id}/accept-intents` | Exact task ID and revision | Seller-only acceptance intent after contract validation |
| `POST /deals/{id}/deliveries` | Canonical schedule artifact, receipt artifact, image ID | 202 validation/relay operation; reject limits/version mismatch; never trust caller's `verified` flag |
| `POST /deals/{id}/refund-intents` | Exact task ID | Eligible transaction intent or 409 with next eligible time and source observation |
| `POST /deals/{id}/execution-authorizations` | Exact quote, limits, expiry, wallet/account reference | Immutable authorization; queues execution only after settled schedule is verified |
| `POST /executions/{id}/stop` | Recorded execution ID and reason | 202 cleanup operation; cannot refund or unpay the schedule |
| `POST /operations/{id}/reconcile` | Reason | Operator-only read/reconciliation job; no implicit second payment/deploy |
| `GET /deals` and `GET /deals/{id}` | Cursor / ID | Scoped summary/detail, status sources, observation age and permitted next actions |
| `GET /deals/{id}/events` | SSE `Last-Event-ID` or cursor | Ordered replay and live stream; polling fallback |
| `GET /artifacts/{id}/download` | Authorized artifact ID | Expiring download grant; no guessed public object key |
| `GET /operations/{id}` | ID | Exact intent, progress, safe error and recovery action |

Return an operation object for async work: `id`, `kind`, `state`, `intentHash`, `resourceId`, `createdAt`, `updatedAt`, `externalRefs`, `nextAction`. Return errors as `{code,message,requestId,operationId?,retryable,fieldErrors?}`. Codes include `QUOTE_EXPIRED`, `TERMS_CHANGED`, `WRONG_NETWORK`, `INSUFFICIENT_FUNDS`, `NOT_REFUNDABLE`, `UNSUPPORTED_CHECKER`, `SPEND_OUTCOME_UNKNOWN`, `ARTIFACT_MISMATCH` and `FORBIDDEN`. Retryability never authorizes a second spend.

Event envelope: `{id,cursor,type,version,workspaceId,dealId,operationId,occurredAt,observedAt,source,data}`. Chain events also carry chain ID, block number/hash, transaction hash, log index and confirmation status. Uniqueness uses chain/transaction/log identity; reconnecting clients reduce by event ID and reconcile from a snapshot cursor. Event payloads are redacted and versioned.

## Additional catalog and settlement endpoints

These proposed routes share the authentication, idempotency, canonicalization and operation-journal requirements above. Use family-specific request schemas and capability checks, not arbitrary calldata supplied by clients.

| Route under `/v1` | Behavior |
| --- | --- |
| `GET /policy-catalog`, `GET /deployments` | Versioned presets, supported rules/modes and verified environment-specific deployments |
| `POST /policies/preview` | Instantiate or import typed policy, validate by family, produce concrete commitment and review summary; no authorization |
| `POST /orders`, `POST /orders/{id}/offer-intents`, `POST /orders/{id}/accept-intents` | Invoice order draft and fully decoded funding/acceptance intents |
| `POST /orders/{id}/invoices`, `POST /invoices/{id}/evaluations` | Store exact document/evidence; re-derive facts and read authoritative order spend |
| `POST /invoices/{id}/settlement-intents` | Explicit mode `proof`, `signature` or `buyer_approval`; verify compatibility and current limits; buyer mode requires buyer authority and separate review |
| `POST /orders/{id}/revoke-signer-intents`, `POST /orders/{id}/close-intents` | Buyer signer revocation or contract-eligible close; verify confirmed effects |
| `POST /vaults/{id}/{control}-intents`, where control is `policy`, `budget`, `pause`, `withdrawal` | Owner-only controls; bind exact next policy version and explain pending-payment impact; pause body explicitly selects pause/unpause |
| `POST /vaults/{id}/payment-requests`, `POST /payment-requests/{id}/settlement-intents` | Native-family evidence verification and correct proof arguments; no generic hash-only transfer |
| `POST /payment-requests/{id}/revocation-intents` | Adapter-specific task cancellation or facts revocation, with exact authority and permanent-ID semantics |
| `POST /vaults/{id}/facts-approval-intents` | Circom-only owner review/registration of exact facts commitment before proving/payment |

All mutations include immutable policy/deployment identity and environment. Proof requests dispatch to pinned task, solver, invoice or Circom tooling. A confirmed old agreement remains on its pinned adapter after a catalog upgrade. Invoice authority, vendor registry, PO signer, reviewer and settlement signer have distinct roles; the site must integrate issuance or authenticated import without treating agent-uploaded unsigned facts as authority.

## Autonomous agent boundary

Wrap the existing `warrant.solver.v1` subprocess protocol behind the seller adapter. It currently exchanges RFQ, quote and work messages; it is not a published general agent interoperability standard. A hosted adapter authenticates each message, binds it to a deal revision and rejects replay or payload changes. The initial deterministic solver can demonstrate agent automation without claiming an LLM optimizer.

An agent authorization specifies chain, escrow, token, recipient constraints, maximum single and cumulative spend, valid time range, permitted tools and maximum active executions. Server policy checks are required before every signing or provider write. A model's request cannot expand its own limits. Revoking a key stops future actions but cannot revoke an accepted escrow's proof settlement.

## Security and acceptance

- BE-1: Restart at every external-write boundary recovers the original operation and causes no duplicate spend or deployment.
- BE-2: Tenant-crossing IDs fail on every object route, artifact grant and event stream; agent keys have narrow scopes and revocation.
- BE-3: CSRF/session protections, rate limits, upload limits and signed-message nonce/expiry checks are covered before a public pilot. Keep signing and io.net secrets in a managed secret service, never browser storage or logs.
- BE-4: Out-of-order/duplicate events, RPC failure and chain reorganization produce correct reconciliation and visible uncertainty.
- BE-5: Signing intents are revalidated just before broadcast against current chain state, committed bytes, deadlines, balances and environment.
- BE-6: Submitted receipt bytes, image ID and public journal are verified before relay; a successful local checker alone is never accepted as a proof.
- BE-7: CAT-1–7 coverage is part of service acceptance; APIs reject incompatible family/mode combinations. Invoice approvals cannot bypass buyer authentication or be requested as an automatic mode fallback.
- BE-8: Proof jobs are isolated by commitment/guest/verifier identity; no 384/416/480-byte or Circom public-signal confusion. Old deployment support remains available for existing obligations.

Provide a generated OpenAPI document and schema-derived client as an implementation deliverable. This document defines behavior; it is not a claim that an OpenAPI validator or service is already shipped.
