# Frontend and interaction design

Proposed v1. [Product scope](README.md) and [payment semantics](settlement.md) govern every screen.

The application serves the [complete policy catalog](policy-catalog.md). The compute screens below are one adapter's views, not the entire product. Policy configuration, funding arrangement and settlement authenticator are separate concepts.

## Information architecture

| Area / route | Primary content | Primary action |
| --- | --- | --- |
| Policies `/policies` | All developed presets/families, trust assumptions, supported modes, deployment readiness | Select policy or import typed policy |
| Deals `/deals` | Title, seller, escrow state, execution state, amount, next action | Create deal |
| Create `/deals/new` | Workload → agreement → quote and review → funding | Review and fund |
| Deal `/deals/:id` | Agreement, independent status lanes, next action, activity | Context-specific recovery or output access |
| Deal: result | Transcript, artifacts, input/output identity, retention expiry | Download result |
| Deal: evidence | Policy, instance, schedule, proof, image ID, chain receipt | Export evidence |
| Agents `/agents` | Configured agents, wallet, supported checker version, permission expiry | Configure bounded authorization |
| Funding `/funding` | One Arc USDC balance, reserved amounts, gas reserve, bridge/provider operations | Resume or reconcile a pending operation |
| Settings `/settings` | Members, roles, API keys, secrets connection status, retention | Manage workspace |
| Operations `/operations` | Stalled workflow, expiring escrow, unknown spend, cleanup queue | Inspect and reconcile |
| Orders `/orders/:id` | Invoice list, funded ceiling, cumulative spend, signer limits and actual settlement mode | Submit invoice, approve explicitly or close when eligible |
| Vaults `/vaults/:id` | Balance versus lifetime allowance, policy version, owner controls and payment requests | Fund, set policy, submit evidence, pause/revoke/withdraw |
| Swaps `/swaps/new`, `/swaps/:id` | Proposed token pair, input, minimum output, route, authority and execution receipt | Review swap intent; unavailable until integration gates pass |

Keep the landing page separate from the signed-in application. A landing page explains the proposition and offers a clearly labeled replay or a real purchase. It must not display sample transactions as live activity.

## Visual direction

Use a calm operations workspace: compact navigation, generous content spacing, strong typography, a restrained green accent and neutral surfaces. The agreement is the central object. Avoid token tickers, decorative AI scores and metrics without a customer action.

The default deal view answers: what was purchased, where the money is, whether execution succeeded, and what happens next. Use text labels with status color. Put cryptographic details behind an Evidence section, while keeping the payment condition visible before funding and beside the receipt. Show Arc and network environment in the application chrome.

Use explicit labels: “Planning fee paid”, “Schedule verified”, “Execution failed”, “Refund available”. Do not compress those into an ambiguous “Verified” or “Complete”. Dates show local time with UTC available; countdowns derive from chain deadlines and current observations, not a client-side assumption that a refund has executed.

## Creation flow

The first step for every new operation is policy selection. Each entry shows what it verifies, the required authorities and evidence, applicable funding arrangements and readiness. Selection changes the parameter form, compatible deployment and evidence adapter, without duplicating identity, transaction review or history. A template ID alone is not a trusted commitment.

For task templates, collect registry/reviewer keys, cap and allowed categories; the specific-deliverable template also requires a recipient and known expected hash. For invoices, collect customer and vendor identity, policy/PO ceilings, authority keys, line/lexicon rules and optional signer terms. The advanced editor/importer exposes every supported rule with family-specific validation and a plain-language preview. Circom has its own fixed fields and owner facts approval; do not show Rust rules there.

The following four steps apply to the compute adapter:

1. **Workload.** Public audio upload with filename, size and hash; template/model revision; optional saved template. Initially retain the pilot's 64 MiB limit. Validate media on the server. Do not allow arbitrary image names or URLs. No upload starts silently in a design preview.
2. **Agreement.** Explain the purchased schedule and the distinction from transcript quality. Advanced fields expose committed jobs, resources, integer tick unit, prices in model units, max cost and deadlines. Show seller wallet, fixed reward and checker version. A natural-language request cannot silently become binding numeric constraints.
3. **Quote and review.** Show each real eligible agent's exact offer, expiration and resources. Include planning fee, duration-based provider estimate, separate execution limit, platform fee if configured, bridge/provider fees and estimated gas. Distinguish amounts charged now, held in escrow, authorized later and variable estimates. If only one agent exists, show one offer.
4. **Funding.** Check chain, balance, allowance, quote freshness and gas reserve. Use exact amount approval by default. Review transactions before wallet signing. A rejected signature returns to review without declaring an error in the agreement. Show approval and escrow funding as distinct operations.

Editing a draft invalidates its quote. Editing terms after an on-chain offer requires a new agreement, with cancellation/refund of the old one only where the contract permits. The interface cannot offer “edit accepted agreement”.

## Deal detail

Render family-specific content. Task payments terminate as paid/refunded. Invoice orders retain an invoice list and remaining balance after partial payment. A revocable vault has no seller acceptance or escrow refund guarantee. The Circom view includes owner-approved facts and its verifier provenance. Swaps show quote/intent/execution status and actual received amounts; a proof or approval alone is not successful execution.

The header contains name, identifier, environment, seller and current next action. Below it, show four independently sourced rows:

| Lane | Possible user-facing labels | Authority |
| --- | --- | --- |
| Agreement / money | Draft, funding pending, offered, accepted, paid, refunded | Confirmed escrow state; pending operations shown separately |
| Schedule / proof | Waiting, computing, proving, proof ready, invalid, published | Prover output and confirmed result event |
| Execution | Not authorized, queued, provisioning, running, collecting result, complete, failed | Durable runner and provider observations |
| Infrastructure | No resources, active, stop requested, stop unconfirmed, stopped | Provider reconciliation |

Separate sections provide the agreement, workload/result, evidence and full activity history. An activity item records event time, observation time, source and status. Do not rewrite “transaction submitted” into “payment confirmed” before chain verification. A chain reorganization or RPC outage downgrades certainty and triggers reconciliation.

The result page exposes the transcript and download after the artifact is durable outside the container. Worker hashes establish artifact identity, not transcript correctness. An expiring download can be renewed with workspace authorization. Read-only sharing never includes secrets or raw provider responses.

## Recovery and required copy

| Situation | Customer message / action |
| --- | --- |
| No quote or unsupported workload | Explain the unsupported constraint; edit draft or contact operator |
| Insufficient USDC or wrong chain | Amount needed includes reserve; switch network or add funds |
| Quote expired | Refresh quote; show changes; obtain new authorization |
| Buyer cancels unaccepted offer | Submit refund; state remains offered until confirmed |
| Seller missed acceptance deadline | Refund available; expired time alone is not a refund transaction |
| Accepted task misses settlement deadline | Refund available strictly after `settleBy`; show network fee |
| Schedule fails checker | No settlement; seller may correct within existing terms and time |
| Cloud run fails after planning fee is paid | “Planning fee paid. Execution failed.” Retry requires a new execution authorization if it spends again |
| Deploy/payment response is lost | “Checking whether this completed.” Do not offer blind retry |
| Bridge is awaiting attestation | Resume the existing transfer; retain source transaction and support reference |
| Provider top-up succeeded, deploy failed | Show credit location and residual balance; reconcile before retry |
| Cleanup acknowledgement only | “Stop requested”; remain actionable until provider confirms stopped |

No “refund everything” action exists when only the planning escrow is refundable. Explain separate provider credits and service terms.

For invoices, show the evaluator decision (Allow, Ask, Deny) separately from the authenticator (Proof, Authorized signer, Buyer approval). The buyer-approval action always opens a dedicated review with invoice amount, vendor, remaining ceiling and override notice, including after Deny. Never treat it as an automatic fallback. Signature settlement is unavailable at the exact proof threshold, after revocation or beyond remaining signer allowance. The UI still offers eligible proof and explicit buyer-approval paths.

For vaults, distinguish token balance from remaining lifetime allowance. Policy rotation and owner withdrawal can invalidate or prevent pending payment; confirmations must explain that effect. For swaps, show spender approval as a separate operation and maintain quote expiry, output minimum, pending/reverted and unknown states as defined in the [swap specification](defi-swaps.md).

## Agent and operator views

Sellers see offered terms, acceptance deadline, committed instance, reward, available proving time, delivery status and payout recipient. A scoped signer accepts only allowed chain/vault/token/amount/deadline combinations. Observed health is timestamped, not a reputation score.

Operators see failed or unknown operations with related transactions, provider IDs, prior attempts and documented actions. Destructive resource controls target the recorded deployment only. Viewers can inspect but cannot sign, spend, change secrets or retry executions. Permission changes do not alter accepted on-chain commitments.

## Interaction acceptance criteria

- FE-1: A new user can create, review, fund and reopen a deal without CLI instructions; human-readable terms match serialized terms exactly.
- FE-2: Closing the browser after acceptance does not stop orchestration; reopening reconstructs state from the backend.
- FE-3: Paid schedule plus failed execution is represented without contradiction. Refresh cannot produce a second spend.
- FE-4: At 320, 768 and 1024 px every primary action remains usable; keyboard navigation, visible focus, labeled inputs, actionable errors and screen-reader status announcements work. Target WCAG 2.2 AA and validate contrast.
- FE-5: Integer token units remain exact through input, formatting, review and signing; long identifiers never break layout.
- FE-6: Live, testnet and replay environments are visually distinct. Sample names, prices and transaction data are labeled. A prototype never invokes a wallet or provider.
- FE-7: Screen states cover empty, loading, stale, forbidden, failure, pending and recovered outcomes; there are no success-only dead ends.
- FE-8: Every catalog entry and existing settlement adapter is reachable; task/order/vault states and proof/signature/approval receipts remain distinct. The catalog shows unavailable deployments without claiming they are live.
- FE-9: Invoice override, signer revocation, partial order closure, vault policy rotation and Circom facts approval have browser-driven acceptance coverage. Swap interactions remain labeled proposed until SWAP release gates pass.

Implementation recommendation: TypeScript UI with a shared typed API client and the team's maintained React stack; wallet adapters must be verified against current Arc documentation when selected. Use server-owned durable state and event streaming with polling fallback. Browser storage may preserve a draft, never keys or authoritative payment state.
