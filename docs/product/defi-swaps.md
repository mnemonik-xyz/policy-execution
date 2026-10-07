# Policy-controlled DeFi swaps

Status: newly specified, not implemented. The previous funding design covered CCTP transfers and provider payments, not swaps. This is a new action family in the same Warrant application. No currently inspected payment contract calls a DEX or enforces received output.

## Product flow

The customer authorizes an agent to exchange an exact input amount for at least a specified output, using a permitted route before an expiry. The website displays the policy, quote, spender, received-token recipient, fees, submission and confirmed actual balance changes. A swap does not require a seller's task acceptance or a reviewer.

Begin with a single-chain, exact-input swap between two allowlisted standard ERC-20 tokens and one reviewed router adapter. Arc is the target environment; the actual DEX, router deployment, tokens, liquidity and RPC behavior must be verified before enabling it. Do not infer availability from Ethereum deployment addresses. Native-token wrapping, exact-output trades, arbitrary aggregator calldata, hooks, lending, cross-chain swaps and recurring strategies are later extensions.

A bridge moves the same asset across networks. A swap changes assets. A journey that needs both uses distinct authorizations and recoverable operations; its cross-chain steps are not atomic.

## Committed authorization

The proposed versioned `SwapPolicy` and `SwapIntent` bind:

- Chain ID, executor address, funding owner/account and policy version/hash.
- Input/output token addresses, exact input in atomic units and minimum **net** output to the named recipient.
- Approved router/adapter identity, spender, supported selector and normalized route/call digest; value fixed to zero for ERC-20-only v1.
- Expiry, unique operation nonce, fee asset, maximum agent/platform fee and fixed fee recipient if a fee is enabled. Any input-token fee must fit inside the authorized total debit; it cannot be an extra unbounded withdrawal.
- Owner authorization and, for delegated mandates, per-trade and cumulative token-specific limits with live on-chain counters and revocation epoch.

Token decimals are display metadata validated against allowlisted assets; never reuse the payment escrow's six-decimal assumption or narrow all swap amounts to u64. Use checked uint256 arithmetic for token amounts. Signing review shows approvals separately from swaps; approving allowance does not mean execution succeeded.

Show estimated output and the absolute minimum output independently. An optional slippage percentage helps derive the minimum from a quote; the actual integer bound is committed. A model-supplied or manipulated quote is not evidence of a fair price. v1 requires the customer to review the minimum for each trade; autonomous recurring mandates require a defined independent price-reference policy with freshness/deviation checks. Do not promise best execution or profit.

## Execution architecture

Introduce a dedicated, reviewed swap executor and a versioned authorization checker/guest if proof-based swap authorization is offered. Define and test a new commitment domain and journal ABI; do not reinterpret the 12-, 13- or 15-word payment journals. Proof verification establishes that an intent satisfies a policy; live checks must still hold when assets move.

For the first implementation, the customer signs a bounded per-operation intent. An agent may prepare or relay it but cannot alter its terms. A general recurring agent mandate is a later policy extension. The UI labels whether authorization uses a customer signature, a bounded delegation or a proof; these are not interchangeable guarantees.

In one transaction the executor must:

1. Authenticate the exact intent and policy, check chain/executor scope, expiry, revocation, unused nonce and live limits.
2. Enter a reentrancy guard, reserve/consume the nonce and budget in state before external calls; a transaction revert rolls those changes back.
3. Pull no more than the authorized input from its owner. Approve only the required amount to the allowlisted spender; construct the reviewed router call from typed fields. No arbitrary target, delegatecall or unreviewed multicall.
4. Execute the swap and validate actual input spend and output balance deltas using the adapter's defined accounting. Do not trust a router return value alone. Handle output through the executor and verify the specified recipient receives at least the minimum net amount after any authorized fees. Reject nonstandard transfer-tax/rebasing tokens in v1.
5. Transfer output and any refundable remainder only to the committed addresses; clear residual router allowance. Include authorized service-fee transfers in the same reverting transaction and emit the settled amounts and intent identity.

If any bound fails, the complete transaction reverts: no successful swap or service fee is recorded, although chain gas may still be spent. Separately mined approval transactions remain approvals and can require revocation. Multiple approvals/submissions are not an atomic user journey merely because the swap transaction is atomic.

The first exact-input adapter should reject partial-fill behavior. If a later router supports partial fills, explicitly version remainder handling and actual-spend accounting. Failed intents can be retried only under their existing unexpired bounds after reconciliation; changed route, recipient, fees or minimum requires a new authorization. A source-chain receipt plus an output expectation is not cross-chain settlement evidence.

## Frontend and API

Add a catalog entry and `/swaps/new`, `/swaps/:id` views. Reuse identity, policy review, signer integration, operation journal, indexing and receipts. Show token pair, input, estimated output, minimum net output, price impact if reliably derived, expiry, gas/fees, recipient and approved spender before signing. No live quote or token balance appears when the integration is only a design fixture.

Proposed endpoints under `/v1`: `POST /swap-quotes` (read-only external quote, no spend), `POST /swap-intents` (validate/prepare immutable intent), `POST /swap-intents/{id}/authorizations` (verify owner signature), `POST /swap-intents/{id}/submit` (idempotent relay), `GET /swap-intents/{id}` and an event stream. Signed payloads must carry domain separation, expiry and nonce; the server revalidates against chain state before broadcast.

States: draft, quote_ready, quote_expired, awaiting_authorization, authorized, submitted, confirming, executed, reverted, expired, revoked, outcome_unknown. Token approval is a separate operation. Cancellation is a submitted revocation transaction with a race until confirmed, not deletion of a web record. A proof-ready indicator must not be presented as a completed swap.

## Release gates

- SWAP-1: Confirm a real target-chain venue/router deployment, verified code/ABI, compatible tokens, working quote route and adequate liquidity; record the manifest. The catalog remains “planned” until this exists.
- SWAP-2: Adversarial tests reject wrong recipient/router/token/chain, stale or replayed intent, malicious calldata, approval abuse, reentrancy, exceeded budget and output below the minimum.
- SWAP-3: A failed router call/output check rolls back funds, fees, nonce and budget changes; separately submitted allowance remains accurately represented.
- SWAP-4: A successful browser-driven test proves actual received output and fees against the intent; test boundary conditions, token decimals and restart/reconciliation without double execution.
- SWAP-5: Independent executor/checker review, measured latency and quote-expiry behavior, verified deployment and capped live validation precede real customer funds. A new proof path additionally needs reproducible guest/journal/verifier tests.

The [Uniswap v3 swap guide](https://developers.uniswap.org/docs/protocols/v3/guides/swapping/single-hop-swapping), checked 2026-10-04, is a reference for exact-input/output bounds, expiry, approval and remainder semantics. Its examples are not production-ready and do not establish Uniswap deployment or liquidity on Arc. Router selection remains open.
