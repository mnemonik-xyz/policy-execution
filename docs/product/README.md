# Warrant product specification

Status: proposed product and interface, 2026-10-04. This specification does not declare a production launch or authorize live spending.

## Product promise

Warrant lets a customer hire an independent agent under explicit, machine-checkable payment conditions. The customer funds an agreement, can disconnect after acceptance, and returns to the delivered result and a verifiable payment receipt.

The first product is **verified compute planning with optional managed execution on io.net**, settled in USDC on Arc. The first useful workload is transcription of public audio. An agent earns its planning fee by publishing a schedule that passes the committed checker. Running the GPU workload is a separate service. A paid planning agreement does not establish that the transcript is correct or that execution completed.

This is a proposed market entry: validate demand with compute operators and developers building agents. A transcription customer who only wants accurate text is unlikely to value a schedule proof by itself. If interviews require payment conditional on a correct transcript, define a new acceptance mechanism before promising that product.

## What makes this a product

| Requirement | Customer outcome | First-release acceptance |
| --- | --- | --- |
| A useful task | Buy an outcome they understand | Public-audio workload, downloadable transcript, original input and output hashes |
| A clear agreement | Understand exactly what earns payment | Plain-language acceptance conditions, fixed recipient and fee, deadlines, public-result notice |
| A complete purchase | See and authorize the cost | Planning fee, separate compute authorization, service fee if any, gas and bridge estimates |
| Autonomous execution | Leave without babysitting agents | Durable orchestration continues after the browser closes |
| Honest status | Know what happened and what to do | Separate proof, escrow, workload and funding status; pending and unknown states retained |
| Recovery | Get eligible money back and stop resources | Contract-correct refund, payment reconciliation, output recovery, confirmed resource cleanup |
| Repeat use | Integrate it into normal work | Workspace, saved templates, deal history, API credentials, receipts, scoped agent permissions |
| Operable service | Get help when automation fails | Support reference, incident queue, spend limits, backups and audited operator actions |

## Release scope

### Include in the first customer pilot

- One workspace with owner, operator and viewer roles; wallet-based transaction authorization is separate from account login.
- Buyer workflow and a curated seller-agent roster. Show the actual configured seller; do not invent a competitive market when only one seller is available.
- One public transcription template, bounded audio upload, explicit retention, fixed worker image and model revision.
- Arc escrow for the planning fee; io.net duration billing for one GPU, one replica, one hour until live evidence supports wider bounds.
- First deployment may use separately authorized, already-funded provider credits. Label this accurately. Autonomous Arc-to-Solana funding is a distinct release gate.
- Result page, evidence bundle, contract-correct refunds, operator recovery and read-only share links with explicit redaction.
- Browser UI and a versioned API using the same backend. The existing subprocess protocol remains a pilot integration, not a claim of A2A compatibility.

### Defer

Open agent registration and reputation, arbitrary workloads, confidential audio, multiple clouds, subscriptions, unrestricted delegated wallets, lending, a token, generalized dispute arbitration and correctness guarantees for model output. RunPod is outside scope.

## Commercial model

Propose a fixed planning bounty and an explicitly quoted execution service. The contract pays the entire bounty to its recorded seller; it does not split a platform fee. Any service fee must appear separately with its own terms and receipt. Its amount is an open commercial decision, not a hardcoded percentage.

An execution quote must identify who pays io.net, who controls the operational wallet, where residual provider credits remain, how unused authorization expires, and which failed-service costs the operator bears. For the initial pilot, use a designated operator with a capped, separately approved execution budget. Self-service managed balances require a wallet custody and billing design review before general availability. Escrow funds cannot finance cloud provisioning.

## Customer journey

1. Choose a task template and supply public audio; review data visibility and retention.
2. Define resource constraints, modeled cost bound, planning fee, acceptance deadline and settlement deadline.
3. Receive a quote from an eligible agent. Inspect terms and the separate execution estimate. Price or scope changes require a new quote and authorization.
4. Review Arc network, seller, USDC amount, allowance and gas reserve; sign funding. The seller accepts the immutable terms.
5. Leave the browser. The seller computes and proves a schedule; a relayer submits proof and result. Confirmed contract state determines payment status.
6. If separately authorized, the runner starts io.net execution using the confirmed published schedule and its approved resource mapping. Collect and persist output, then terminate resources.
7. Reopen the deal to retrieve the transcript, inspect the receipt or take the specific recovery action offered.

An invalid schedule never becomes payable merely because a transcript was produced. A valid paid schedule can coexist with a failed cloud run; the UI must show both.

## Specification map

- [Frontend and interaction design](frontend.md)
- [Backend, API and agents](architecture-api.md)
- [Escrow, proof and trust boundary](settlement.md)
- [io.net execution and cross-chain funding](execution-funding.md)
- [Delivery plan, operations and release gates](delivery-plan.md)

## Evidence baseline

| Component | Observed baseline | Remaining work |
| --- | --- | --- |
| Solver checker, escrow and local proof demo | Merged in `policy-execution` at `b85532c003c43597adda3249d7b11a69f13b6c2d` through PR #4 | Arc deployment and independent security review |
| io.net adapter, worker and payment-plan parser | Proposed in [PR #5](https://github.com/mnemonik-xyz/policy-execution/pull/5), head `5722a6e7f9a2c700aed38846e0e975a5713be80e` | Image build, real GPU run, orchestration, actual bridge and payment execution |
| Frontend, hosted API, durable workflow service | Specified here | Implementation and end-to-end validation |
| Arc verifier and escrow deployment | Not established by the inspected evidence | Verified addresses, bytecode, image ID and a real proof settlement |
| Live customer purchase | Not demonstrated | Capped pilot with evidence and cleanup |

The io.net pilot's 19 offline tests validate adapter/worker behavior with mocked cloud/model services, including a local MCP exchange. They are not evidence of a paid GPU deployment. Existing local solver tests and proofs are not evidence of Arc deployment.

At inspection, parent repository pins lagged this implementation: `warrant/main` pointed to policy execution `c589871…`, and `tameion/main` pointed to warrant `50fa9e1…`. Update and verify release pins deliberately after component merges; do not treat a parent checkout as the latest application.
