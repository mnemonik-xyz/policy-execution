# Strict invoice payment assurance

Status: implementation and local verification work, **not production approval**.
Verification scope reviewed 2026-10-09. No public deployment is authorized by this document.

## Runtime architecture

`ProofInvoiceFactory` creates `ProofInvoiceEscrow` instances with a fixed token
and verifier. Each escrow pins one guest image ID. A customer funds an order only
after approving that image and the policy commitment. Creation is permissionless;
the factory does not endorse arbitrary guest images.

The strict escrow rejects `settleSigned` and `settleApproved`, including calls
from the funding customer. Offers cannot configure a signer. The internal payment
boundary also requires `Authenticator.Proof`. Refunds remain possible under the
existing acceptance and timeout rules; they are not policy payments.

The shared factory records consumed `(customer, obligationId)` pairs. Only its
own newly created escrow code can consume an obligation. Registration and
consumption have no administrator or reset operation. Token and verifier are fixed
for the family. Consumption, escrow accounting and transfer are one transaction:
a failed transfer rolls all three back.

For a new guest image, create another escrow through the **same factory**. This
preserves replay history; it does not move existing orders or funds. A different
factory, another chain, legacy InvoiceEscrow, or a vault is a different replay
domain. Migrating those domains is not implemented or claimed safe. Existing
obligations must not be reintroduced there as unpaid.

The legacy mixed-mode `InvoiceEscrow` remains available. Its signature and buyer
approval paths do not have the strict guarantee. `PolicyExecutionVault.withdraw`
also remains owner-authorized and outside its `pay` policy guarantee.

## What Verus proves

The executable evaluator and its specifications share `verified/src/lib.rs`.
The invoice evidence adapter now also calls these executable verified functions:

| Function | Property |
| --- | --- |
| `decide` | Decisive results agree with the policy for every completion of unknown facts |
| `authorize_payment` | A returned payment requires Allow, positive amount and a nonempty validity window; recipient, amount and document are exactly those evaluated |
| `intersect_window` | Returned bounds admit exactly the intersection of the supplied validity windows |
| `convert_amount` | Conversion is floor(minor units × numerator / denominator), rejects zero and out-of-u64 results, and multiplication cannot overflow u128 |

The native invoice path and guest use the same implementation. The guest commits
a journal only after Allow. Formal verification is separate from compilation:
`verified/verify.py --mutations` invokes the pinned Verus with `--no-cheating`,
prints the source digest, and requires deliberate executable bugs to fail proof.

These theorems do **not** prove authentication, XML parsing, classification,
facts projection, policy validation, commitment calculation or complete journal
encoding. Signed evidence can also be false. Those are explicit remaining gaps,
not properties supplied by the execution proof.

## Solidity properties and their limits

`contracts/test/ProofInvoiceEscrow.t.sol` contains regression/fuzz tests and
Halmos `check_*` properties for proof gating, exact transfer, domain binding,
two-payment ceiling enforcement, replay, failed-transfer atomicity, post-close
rejection, and registry access control. Successful-path checks assert success
explicitly to avoid vacuous proofs. Domain binding is a safety implication;
it does not assert that every valid proof must settle.

These run the actual escrow/factory bytecode against the test ERC-20 and a
selective mock verifier. The verifier is an **explicit assumption**, not a
cryptographic proof in Halmos. SHA-256 is an uninterpreted function. These checks
are finite transition scenarios over symbolic arguments, not an inductive proof
over every possible transaction history or arbitrary token implementations.
Cross-escrow replay is additionally exercised by a concrete regression.

Halmos is pinned to 0.3.3. `scripts/run-halmos.py` applies a disclosed, in-memory
compatibility correction: upstream SHA-256 input sorts use byte counts where Z3
requires bit counts. The wrapper changes `arg_size` to `arg_size * 8`; it does not
change bytecode, suppress assertions or treat errors as successes. It refuses an
unexpected Halmos version/source pattern. The formal Foundry profile disables
dynamic test linking, which Halmos does not support.

```sh
python3 -m venv /tmp/warrant-formal
/tmp/warrant-formal/bin/pip install halmos==0.3.3
FOUNDRY_PROFILE=formal /tmp/warrant-formal/bin/python scripts/run-halmos.py \
  --root contracts --forge-build-out out-formal --contract ProofInvoiceEscrowTest \
  --solver z3 --solver-timeout-assertion 30000 --json-output artifacts/symbolic.json
```

## Binding source to the accepted program

`methods/build.rs` pins the reproducible guest builder by Docker digest. The
`warrant-host invoice-build-info` command recomputes the embedded ELF's image ID,
checks it against the generated constant, and emits the ELF hash, guest name,
checker version and journal schema. It rejects an absent/invalid guest ELF.

`scripts/proof-release.py prepare` runs Verus, native tests, Solidity tests,
the complete enumerated symbolic suite, a Docker guest build, and a fresh local
strict proof settlement. It rejects simulated proof/skip-build environment
settings, zero/wrong guest identities, missing symbolic properties, failed
properties, bounded loops, source changes during the run, wrong-image acceptance,
journal tampering and replay. It records source hashes (including uncommitted
additions), lockfiles, tool versions, host/guest identities, evidence hashes and
contract artifact hashes in a candidate manifest. No manifest is produced if a
required step fails.

```sh
python3 scripts/proof-release.py prepare \
  --verus /path/to/pinned/verus --formal-python /tmp/warrant-formal/bin/python
python3 scripts/proof-release.py check artifacts/proof-release-.../manifest.json
```

`check` detects drift and prints the candidate's deployment settings. It does not
sign or authenticate a manifest: the operator must approve its origin and verify
the recorded evidence. It does not broadcast, inspect a deployed contract or
claim compiler correctness. Source-to-binary compilation and the RISC Zero proof
system remain trusted. Candidate manifests explicitly set
`endToEndFormallyVerified: false` and list unfinished obligations.

For a fresh local proof-only run without preparing a full release candidate:

```sh
RISC0_USE_DOCKER=1 python3 scripts/invoice-demo.py --proof-only
```

The demo uses Anvil's public test keys, synthetic invoices and a real verifier.
It checks deployed image/token/verifier/registry bindings and writes proof and
settlement evidence. It is not a public-network or real-invoice acceptance test.

For a **new replay domain**, `DeployInvoice.s.sol` accepts
`WARRANT_PROOF_ONLY=true` and creates a factory and its initial escrow. For an
**existing replay domain**, call the existing factory's `createEscrow(imageId)`;
do not run a fresh-factory deployment and assume its history is inherited.

## Remaining production gates

1. Verify the evidence-to-facts pipeline or adopt a reviewed canonical signed-facts
   boundary; formalize the remaining authorization bindings and journal encoding.
2. Prove contract invariants inductively, covering funding, acceptance, settlement,
   refunds, cross-escrow consumption, callbacks and supported token assumptions.
3. Independently review the specifications, tool compatibility patch, compiler/
   image binding and complete cryptographic verifier trust chain.
4. Validate the approved artifacts on the target chain, with code and constructor
   read-back, a funded lifecycle and operational recovery procedures.

Rocq evaluation is deferred until this implementation is completed. A future
tool choice does not change the runtime proof/image/journal binding requirement.

## Primary references

- [Verus trusted components](https://verus-lang.github.io/verus/guide/tcb.html)
- [RISC Zero receipt and image verification](https://dev.risczero.com/api/zkvm)
- [Halmos symbolic test scope](https://github.com/a16z/halmos/blob/main/docs/getting-started.md)
