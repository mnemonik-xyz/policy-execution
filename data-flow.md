# Policy execution: actors, evidence and proof

## Example request

> Pay a registered contractor up to 100 USDC after our designated reviewer accepts
> the deliverable.

The owner approves the rules and registry/reviewer keys in advance. The contractor
can be selected later. The registry authenticates that contractor, and the reviewer
signs acceptance bound to the same task, deliverable, recipient and amount.

## Data-flow diagram

```mermaid
sequenceDiagram
    autonumber
    actor Owner as Business owner
    participant Agent as Untrusted agent
    participant Registry as Approved vendor registry
    participant Reviewer as Approved acceptance reviewer
    participant Prover as Owner-controlled prover
    participant Guest as Fixed policy interpreter in zkVM
    participant Check as Local receipt verifier
    participant Vault as Payment vault - planned
    participant Token as Token contract - planned

    Note over Owner,Prover: POLICY APPROVAL
    Owner->>Prover: Reviewed policy, issuer keys, scope and validity interval
    Prover->>Prover: Compute policy commitment
    Owner-->>Vault: Planned: pin approved policy hash and version; fund budget
    Note over Prover,Guest: One interpreter handles supported rule combinations.<br/>Changing policy data does not require a new program.

    Note over Agent,Reviewer: TASK AND EVIDENCE
    Agent->>Agent: Select contractor and propose task payment
    Agent->>Registry: Request vendor credential
    Registry-->>Agent: Signed recipient, category, scope and validity
    Agent->>Reviewer: Present task and deliverable for acceptance
    Reviewer-->>Agent: Signed task ID, deliverable hash, recipient, amount, result and validity
    Note right of Reviewer: Reviewer evaluates acceptance.<br/>The proof authenticates this statement.<br/>It does not establish objective work quality.

    Note over Prover,Check: POLICY EXECUTION AND PROOF
    Agent->>Prover: Payment proposal plus signed evidence
    Prover->>Guest: Private policy, request and evidence
    Guest->>Guest: Validate bounded policy structure
    Guest->>Guest: Verify signatures using policy-approved keys
    Guest->>Guest: Match evidence to payment, task, deliverable and domain
    Guest->>Guest: Intersect policy and evidence validity intervals
    Guest->>Guest: Evaluate all / any and supported predicates
    Note right of Guest: Shared evaluator has a Verus correctness proof.<br/>Evidence checks and surrounding authorization code are outside that proof.
    alt Invalid evidence or policy denies
        Guest-->>Prover: Abort without an authorization journal
        Prover-->>Agent: Reject payment proposal
    else Policy allows
        Guest-->>Prover: Public authorization journal
        Prover->>Prover: Generate cryptographic execution proof
        Prover->>Check: Receipt plus expected interpreter image ID
        Check->>Check: Verify proof and its binding to the public journal
        Check-->>Prover: Valid receipt or verification failure
    end

    rect rgb(242, 242, 242)
        Note over Agent,Token: PLANNED PAYMENT INTEGRATION
        Prover-->>Agent: EVM-compatible proof plus public authorization
        Agent-->>Vault: Submit proof and authorization
        Vault->>Vault: Verify proof against approved interpreter image
        Vault->>Vault: Check active policy, domain, current time and remaining budget
        Vault->>Vault: Reject cancelled or already-paid task ID
        alt Every check passes
            Vault->>Vault: Consume task ID and update spend atomically
            Vault->>Token: Transfer exact amount to proof-bound recipient
            Note over Vault,Token: Transfer failure must revert consumption and spend too
        else Any check fails
            Vault-->>Agent: Revert without payment
        end
    end
```

## Implementation status

The current executable is a local CLI. As of 2026-09-22, all 24 tests pass:
17 native interpreter tests, 4 guest execution tests and 3 receipt verification
tests. Two real succinct receipts for different policies and recipients verify
against the same interpreter image. Receipt tests reject modified authorization
fields and a wrong interpreter image. The earlier Circom prototype's tests are
separate. Verus verifies the shared evaluator against declarative semantics
(7 verified, 0 errors); eight executable mutations fail verification. This does
not cover the surrounding authorization pipeline. See the
[verification results](verified/verification-results.md).

Registry/reviewer credentials are signed test fixtures. Agent and issuer services,
owner approval UI and a separate prover service are not implemented. The shaded
payment section is a future integration: this CLI does not produce EVM-ready
proofs or submit payments. The arrows show responsibilities and the intended data
flow, not a claim that all participants are deployed services.

## What the proof establishes

The interpreter authenticates evidence and evaluates the policy before emitting a
payment authorization. A verified receipt binds this computation to the
fixed interpreter image and its public output. The owner-approved policy hash
must match that output before payment. Adding unsupported operations requires a
reviewed interpreter upgrade; changing supported rules only changes policy data.

The owner must approve an exact policy, including who can attest acceptance.
Proving that a reviewer signed a statement does not establish that it is true.
Likewise, the proof does not establish that the rules capture the user's entire
natural-language intent. The shared rule evaluator has a
[Verus proof against declarative semantics](verified/README.md); the surrounding
authorization pipeline is not formally verified.

## Who can see what?

| Data | Agent | Owner-controlled prover | Public verifier |
|---|---|---|---|
| Full policy and issuer keys | Optional | Yes | Policy hash; values may be inferred |
| Recipient, amount and task ID | Yes | Yes | Yes |
| Signed vendor credential and acceptance statement | Yes in this flow | Yes | Evidence commitment |
| Deliverable bytes | According to task permissions | Not needed by this interpreter | Not published |
| Deliverable hash | Yes | Yes | Yes |
| Scope, policy version and authorization time bounds | Available with authorization | Yes | Yes |

ZK privacy is relative to the verifier. The prover sees its private inputs. Repeated
requests and their outcomes can reveal aspects of a hidden policy; the design does
not promise resistance to such inference or payment anonymity.

## Implementation references

- [Policy types, evidence checks and interpreter](core/src/lib.rs)
- [Fixed zkVM guest](methods/guest/src/main.rs)
- [Host execution, proving and verification CLI](host/src/main.rs)
- [Interpreter tests](core/tests/policy.rs)
- [Receipt tests requiring completed real proofs](host/tests/receipts.rs)
- [Build instructions and remaining limitations](README.md)
