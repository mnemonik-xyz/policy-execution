# io.net execution and cross-chain funding

Proposed product integration based on [pilot PR #5](https://github.com/mnemonik-xyz/policy-execution/pull/5). The pilot implements bounded deployment and a payment plan, not automatic bridging or payment.

This is the optional compute adapter of the multi-policy site. Invoice, contractor, vault and Circom settlement do not require an io.net account or transcription workload. CCTP funding is distinct from the newly proposed [DeFi swap flow](defi-swaps.md); neither a provider payment nor a bridge receipt establishes swap execution.

## Resource mapping and billing

Commit a mapping from each solver machine to a provider hardware/location configuration, worker image digest and unit/tick definition. Record duration estimates and how they were obtained. The runner must reject schedules outside its supported template rather than silently reinterpret them.

The initial live run uses one GPU, one replica and one hour of duration billing. Discover actual integer CaaS hardware and location IDs, input schemas, availability and estimates through the documented MCP endpoint. Never invent a GPU inventory. The first transcript validates useful execution; a one-resource demo alone does not establish scheduler savings. Expand to multiple jobs/resources only after mapping, isolation and billing are validated.

The solver's job-duration cost can differ from rental charges because providers charge for reserved time, startup, minimum periods and idle time. Display “modeled schedule cost” and “provider estimate” separately. A pricing-aware proof of the entire cloud bill would require a versioned checker and trustworthy price inputs.

The pilot matches hardware, location, duration, GPU count and replica count between estimate and deploy and rejects estimates older than five minutes or over the authorized bound. This checks an estimate; it is not an atomic provider-enforced cap. Keep available credits bounded, reconcile actual charges and stop provisioning when uncertainty exists. PayG remains disabled for the pilot because duration does not bound PayG lifetime.

## Durable execution flow

1. Require a confirmed paid planning agreement, decoded schedule matching the published hash, verified resource mapping and an unexpired separate execution authorization.
2. Check availability and obtain a fresh estimate. If resources, price or validity change beyond authorized terms, request new authorization; do not rewrite the accepted schedule.
3. Persist deployment intent before making the provider write. Use the recorded identifier for every subsequent read and cleanup.
4. Reconcile deployment and endpoint readiness. A process health check is not proof that GPU/model initialization succeeded.
5. Upload the authorized bytes with content length and SHA-256 to the authenticated worker. The pilot's single-job worker returns the existing job for repeated identical input and rejects a different input.
6. Collect transcript, input/output digests, model revision, timestamps and execution observations to durable external storage. Distinguish observations from cryptographic execution proof.
7. Request destroy on the recorded deployment. Poll until stopped/terminated is independently confirmed. Report failure or uncertainty to the operator; do not close cleanup on acknowledgement alone.

Each step survives process and host restart. A lost deploy response is `outcome_unknown`, not permission to deploy again. The production service must reconcile by provider account, immutable request attributes and known IDs; where it cannot establish identity, stop and escalate. Preserve exactly what is known.

## Funding topology

The bounty stays in Arc escrow. io.net execution uses a separate budget/account. Current provider documentation describes x402 USDC payment requests on Solana; the application must read network, token, recipient and exact amount from the fresh response and validate an allowlisted route.

Circle lists Arc as CCTP domain 26 and Solana as domain 5, on mainnet and testnet. Domain IDs are not chain IDs. Bridge to the authorized operational wallet first, then make the provider's exact single transfer. Do not assume a direct cross-chain mint to the provider will satisfy its matching rules.

| Stage | Durable states | Completion evidence |
| --- | --- | --- |
| Bridge quote | quoted, expired | Fee quote, gross burn and minimum net amount |
| Source | prepared, awaiting_signature, burn_submitted, burn_confirmed, outcome_unknown | Verified source receipt and message identity |
| Attestation | attestation_pending, attested | Valid attestation for the recorded message |
| Destination | mint_submitted, mint_confirmed, outcome_unknown | Verified mint and usable destination balance |
| Provider payment | payment_prepared, payment_submitted, payment_confirmed, outcome_unknown | Exact network/mint/recipient/amount and confirmed transaction |
| Credits | credit_pending, credited, reconciliation_required | Provider credit/account evidence tied to the payment |
| Deployment continuation | revalidated, resumed, stopped | Fresh checks and one reconciled deploy outcome |

Persist transaction nonces/signatures and signed payload identity before submission. A timeout resumes the existing operation or checks its status. Never burn or pay again to “unstick” an unknown outcome. Expired attestations/quotes, already-used messages, destination fee shortages and RPC outages have specific recovery paths; they never automatically reset the workflow to its first step.

A bridge quote must cover fees while leaving enough net USDC for the full payment. Arrange Solana transaction fees or a configured sponsor separately. Require chain/environment consistency; testnet USDC cannot buy live compute. Surface minimum top-ups, provider fees and residual credits before authorization. CCTP and provider transactions are not atomic with escrow settlement.

## Wallet and permission boundary

Buyer funding and seller acceptance each require the respective party's explicit wallet signature or previously granted, bounded agent policy. Funding executes as the buyer; `TaskEscrow.accept` must execute as the recorded seller/recipient, matching the seller-only acceptance intent in the API specification. A buyer's authorization cannot substitute for the seller's acceptance authority.

Operational signers live in a secret-backed service with per-deal, per-day, token, chain and recipient limits. Separate provider credentials from worker tokens and chain keys. A provider payment request is untrusted input until schema, endpoint, route, asset, amount and current intent match.

The initial pilot can use operator-provided credits with a recorded cap. This proves the compute path but does not satisfy the automated-funding release criterion. Do not expose a top-up control until the end-to-end signer, fee accounting and recovery service exists.

## Integration acceptance criteria

- CLOUD-1: An immutable worker image runs a known public audio fixture on a real io.net GPU; record provider ID, output, digest and observed charges, then confirm cleanup.
- CLOUD-2: Wrong input hash, duplicate job, worker restart, failed initialization, unavailable hardware and interrupted collection yield explicit outcomes without duplicate jobs or hidden resource leakage.
- CLOUD-3: The runner consumes the confirmed committed schedule and resource mapping; evidence shows that mapping, rather than an unrelated cloud demo.
- FUND-1: A capped Arc-to-Solana transfer completes with recorded gross fees, net receipt and exactly one exact provider payment; credit confirmation precedes deployment continuation.
- FUND-2: Restart after burn, while awaiting attestation, after mint submission, after payment submission and during credit reconciliation preserves identity and causes no duplicate charge.
- FUND-3: Changed/expired payment request, wrong network/mint/recipient, below-minimum net receipt and exceeded authorization fail closed and explain recovery.
- FUND-4: Residual funds and provider credits remain attributable; the receipt separates bounty, compute, bridge, provider, platform and network fees without double-counting.

## Primary sources

Checked 2026-10-04. Recheck live schemas and payment routes during implementation.

- [io.net Agent Cloud](https://io.net/docs/guides/clouds/agent-cloud)
- [io.net CaaS deployment](https://io.net/docs/reference/caas/deploy-a-container)
- [io.net price estimation](https://io.net/docs/reference/caas/price-estimation)
- [Circle supported CCTP domains](https://developers.circle.com/cctp/concepts/supported-chains-and-domains)
- [Arc App Kit bridge](https://docs.arc.io/app-kit/bridge)
- [Circle USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)
