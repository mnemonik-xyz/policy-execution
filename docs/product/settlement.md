# Escrow, proof and trust boundary

Baseline: merged commit `b85532c003c43597adda3249d7b11a69f13b6c2d`. Source links: [TaskEscrow](../../contracts/src/TaskEscrow.sol), [SolverBountyEscrow](../../contracts/src/SolverBountyEscrow.sol), [solver checker](../../core/src/solver.rs), [solver demo](../../solver-bounty/README.md).

This document details the solver agreement. The [complete catalog](policy-catalog.md) defines all other supported policy families, funding arrangements, authenticators and their exact entry points. They are required site scope, not deferred compute extensions. Only the new [swap executor](defi-swaps.md) is a proposed execution family.

## What is proven

A pinned solver guest checks that a submitted schedule satisfies the committed instance: job compatibility, capacity, releases, deadlines, dependency order, canonical assignments and checked arithmetic. It computes modeled cost and checks the committed bound and policy. Settlement verifies the real receipt against the deployment's immutable image ID, binds the authorization to the escrow and pays the recorded recipient.

The proof does not establish that durations or resource prices are true, that the schedule is optimal, that hardware was rented, that a transcript is correct, that the cloud met a deadline or that the modeled cost equals the bill. The shared policy evaluator's formal verification does not extend automatically to the new solver checker, projection or Solidity contracts.

## Current limits and encodings

| Field | Current requirement |
| --- | --- |
| Checker | Version 1 |
| Instance | At most 128 jobs, 16 machines, 16 dependencies per job |
| Dependencies | Strictly sorted prior job indexes |
| Assignments | Exactly one entry per job in canonical job order |
| Execution model | Non-preemptive, half-open time intervals; zero duration means incompatible machine |
| Cost | Sum of duration × units × price per unit tick, with checked overflow |
| Result bytes | ASCII `warrant/schedule/v1` followed by big-endian u32 count, then u32 job, u32 machine, u64 start per assignment |
| Solver journal | Exactly 13 ABI words / 416 bytes, including total cost |
| Result binding | SHA-256 of exact published result bytes equals the journal's deliverable hash |
| Settlement entry | `settleWithResult`; inherited hash-only `settle` rejects |

Canonical hashes and serialization must reuse the versioned Rust implementation. Do not invent a browser JSON canonicalization rule. Keep the guest image, checker version, schema version, contract address and chain in deployment metadata and the evidence bundle.

## Exact escrow lifecycle

`taskIdFor(customer, salt)` binds chain ID, escrow address, customer and salt. `offer` reserves the fixed token amount immediately and enforces a positive amount, nonzero policy/version, valid recipient, `acceptBy >= block.timestamp` and `settleBy > acceptBy`. Terms have no update method.

| From | Action | Who / time condition | Result |
| --- | --- | --- | --- |
| Missing | Offer and fund | Customer with adequate token allowance and balance | Offered |
| Offered | Accept | Recorded seller; chain time <= acceptBy | Accepted |
| Offered | Refund | Customer at any time; anyone strictly after acceptBy | Refunded to customer |
| Accepted | Settle with proof and result | Anyone relays; chain time <= settleBy and within proof validity | Paid to fixed seller |
| Accepted | Refund | Anyone; chain time strictly > settleBy | Refunded to customer |
| Paid / Refunded | Further settlement or refund | Disallowed | Terminal |

At exactly `settleBy`, settlement is still allowed and accepted-task refund is not. Offer cancellation and seller acceptance can race before `acceptBy`; final chain ordering decides the outcome. The frontend must refresh state after a failed action. It cannot promise cancellation until confirmed.

The buyer can disconnect after funding and seller acceptance. Relayers cannot redirect payment. A paid task has no contract refund for a later io.net failure. There is no admin rescue or arbitration path in this escrow; operational escalation cannot rewrite its rules.

## Publication and privacy

Successful settlement publishes the schedule bytes in the transaction and `ResultPublished` event. Bytes may also become visible in transaction propagation or reverted calldata before successful payment. This is public delivery with proof-gated payment, not confidential fair exchange. Do not put secrets, private audio, authentication data or sensitive identifiers in the schedule, policy or journal.

Keep transcripts in access-controlled object storage unless the customer explicitly chooses publication. The initial pilot accepts public audio only. Data deletion can remove hosted artifacts but cannot remove chain history. A shareable evidence bundle needs a redaction review; raw provider responses may contain environment variables or account data.

## Arc deployment requirements

Official Arc documentation checked 2026-10-04 lists mainnet chain ID 5042 and testnet 5042002. USDC ERC-20 is `0x3600000000000000000000000000000000000000`, with six token decimals; native gas uses 18 decimals. These are views of the same balance and must not be added together.

Before offering real deals, verify the RPC chain, token behavior/decimals, deployed verifier code and accepted proof format, pinned guest image, escrow bytecode/constructor parameters and a real proof settlement. Publish a deployment manifest with source commit, compiler/build inputs, addresses, code hashes, chain and verification transaction. Standard local Anvil success does not establish Arc-specific behavior.

## Settlement acceptance criteria

- CHAIN-1: Real proof settles the exact authorized result on Arc testnet; receipt identifies all deployment parameters.
- CHAIN-2: Wrong image, changed journal field, result mismatch, wrong scope/recipient, stale validity, duplicate settlement and hash-only settlement fail without payout.
- CHAIN-3: Refund behavior passes tests before, at and after both deadlines and across acceptance/cancellation races.
- CHAIN-4: Six-decimal token and 18-decimal gas handling preserve exact balances; wallet funding leaves an explicit gas reserve.
- CHAIN-5: Independent review covers the solver checker, projection, escrow, proving build and signing boundary. Mainnet rollout requires recorded resolution of findings.
- CHAIN-6: Each enabled task, invoice and revocable-vault deployment has its own verified manifest and settlement evidence; the Circom deployment uses its own reviewed key/setup. CAT coverage prevents presenting a solver-only deployment as complete site support.

## Recorded exception for the Arc mainnet canary

Recorded 2026-10-14. Decision of the core team on 2026-10-09, in the
[canary plan](https://github.com/mnemonik-xyz/warrant-canary-deployment/blob/main/docs/mainnet-canary-plan.md).

- CHAIN-1 is not satisfied. No real proof has settled on Arc testnet. The canary settles
  one capped order on Arc mainnet without a prior testnet settlement.
- CHAIN-5 is not satisfied. No independent review is planned before the canary. A
  self-review in [`solver-bounty/canary/self-review.md`](https://github.com/mnemonik-xyz/warrant-canary-deployment/blob/main/solver-bounty/canary/self-review.md) replaces it for the canary only.
- The exception applies to the canary only: one order, at most 10 USDC, on the solver path.
  It does not apply to a production launch. CHAIN-1 and CHAIN-5 remain open gates for P5.
- Extension of 2026-10-15: the exception also covers the `TaskEscrow` seller service on
  chain 5042. Its scope is the task set: `ReceiptClaimVerifier` and `TaskEscrow` pinned to the
  Warrant guest image ID. `ReceiptClaimVerifier` verifies a 101-byte cryptographic receipt seal
  (`SELECTOR || claimDigest || signature`) signed by the authorized prover over the RISC Zero
  `ReceiptClaim` digest, chain ID, and verifier address, without a Groth16 SNARK proof. Seller
  policy limits each deal to at most 1.00 USD and 1 hour, and allows one deal at a time.
  These limits are seller policy, not contract rules.
- Trust facts of the seller service: `ReceiptClaimVerifier` requires the authorized prover's
  secp256k1 signature over the exact `(chainId, verifier, imageId, journalDigest)` receipt claim,
  so unauthorized third parties cannot forge a seal. Because the seller controls the prover key
  for the canary, the buyer trusts the seller not to sign a receipt claim without buyer
  acceptance. The extension does not apply to a production launch either.
- CHAIN-6 for the task set: the deployment record `task-escrow.json` holds the Foundry
  broadcast and the `cast` checks, and the deployment PR publishes it. No settlement
  evidence exists before the first buyer deal, so CHAIN-6 stays open for the task set.
- Evidence in place of CHAIN-1 has three parts. The first is the real Groth16 settlement on
  private Anvil in CI ([`solver-bounty/ci-result.json`](https://github.com/mnemonik-xyz/warrant-canary-deployment/blob/main/solver-bounty/ci-result.json)). The second is the read-only mainnet
  probe of the canary plan item 4. The third is the local rehearsal record
  `solver-bounty/canary/local/<UTC timestamp>/rehearsal.md` with its `rehearsal.json`, in [warrant-canary-deployment](https://github.com/mnemonik-xyz/warrant-canary-deployment/blob/main/solver-bounty/canary).

Sources: [Arc network configuration](https://docs.arc.io/arc/references/connect-to-arc), [Arc contract addresses](https://docs.arc.io/arc/references/contract-addresses), [Arc EVM differences](https://docs.arc.io/integrate/evm-differences).
