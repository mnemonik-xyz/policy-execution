# Proof payment implementation handoff

Checkpoint: 2026-10-09. Repository: `mnemonik-xyz/policy-execution`.
Continue the current work before considering a Rocq redesign. The user wants
production-quality proof-authorized settlement and precise disclosure of gaps.
This checkpoint is **not production-ready** and is not a deployment approval.

## Implemented

- Strict `ProofInvoiceEscrow`: no signature or buyer-approval payment bypass;
  original mixed-mode contract remains available.
- `ProofInvoiceFactory`: immutable token/verifier family, permissionless escrow
  creation and shared customer/obligation replay history. Reuse the same factory
  across image changes. Different factories/legacy contracts do not share history.
- Actual runtime calls to Verus-verified payment-field construction, validity
  intersection and integer currency conversion, alongside the existing evaluator.
- Explicit policy/customer binding checks in invoice settlement.
- Symbolic properties over actual Solidity bytecode, with disclosed mock verifier
  and standard test-token models. See `formal-payment-assurance.md` for limits.
- Pinned Docker guest-builder digest and `invoice-build-info`, which recomputes
  the embedded ELF image ID and exports build identity/schema information.
- Strict local proof demo and fail-closed candidate release manifest workflow.

## Completed checks on this checkpoint's source

| Check | Result |
| --- | --- |
| `cargo test -p warrant-policy --locked --offline` | 76 tests passed |
| `forge test --root contracts --offline` | 82 tests passed; includes historical real-proof fixture |
| Pinned Verus, `--no-cheating` | 20 obligations verified, 0 errors |
| Executable mutation checks | 19 of 19 rejected by proof failure |
| Halmos 0.3.3, Z3, strict escrow | 8 symbolic properties passed, no bounded loops |
| Release gate unit tests | 4 passed |

Verified `verified/src/lib.rs` SHA-256:
`ba51cb272c5d910de1c33c59f436d83fa697ade89f6949539e839684e09dfb67`.
Re-run after source changes. The historical real-proof fixture does not establish
fresh settlement of the current guest.

## Required continuation

1. Review the diff, particularly cross-escrow replay, the final payment constructor
   integration and the release runner. Add missing adversarial tests as needed.
2. Finish a **fresh** pinned Docker build and strict real-proof settlement. An
   earlier Docker build completed locally, but subsequent conversion/pinning and
   CLI argument-guard changes invalidate it as final evidence. Do not reuse its ID.
3. Exercise `scripts/proof-release.py prepare` end to end, then `check`; fix failures
   without weakening evidence gates. The runner and updated strict demo have not
   yet completed together. In particular verify Forge's emitted factory creation
   event parsing and whether the toolchain emits the expected guest ELF form.
4. Add and exercise deployment read-back against the approved contract bytecode,
   token, verifier, image ID and replay registry. Current `check` is local only.
5. Preserve an explicit remaining-gaps list. Complete evidence-to-facts and journal
   refinement proofs and inductive contract invariants before claiming whole-flow
   formal verification. Arrange independent review and target-chain validation
   separately; do not deploy production or move real funds from this handoff.

## Tools and reproduction

Use Linux x86-64 with Docker available for reproducible guest builds and Groth16
wrapping. Check the cloud runtime's actual capabilities before assuming Docker or
enough proving memory. Install Rust 1.98.1 (workspace toolchain), RISC Zero Rust
1.88.0 / r0vm 3.0.5, Foundry with solc 0.8.28, Python 3.12 and Node/npm.
All demo signing keys are public Anvil fixture keys; no real wallet keys are needed.

Verus release: `0.2026.09.20.aef82ed`. Linux asset:
`https://github.com/verus-lang/verus/releases/download/release/0.2026.09.20.aef82ed/verus-0.2026.09.20.aef82ed-x86-linux.zip`

Expected ZIP SHA-256:
`7b870fa12bc589015c2fab60a8b3d9f07c7b1adb3444eb0fadffcbf7f0447b33`.

```sh
python3 -m venv /tmp/warrant-formal
/tmp/warrant-formal/bin/pip install halmos==0.3.3
python3 verified/verify.py --verus /path/to/verus --mutations
cargo test --locked -p warrant-policy
npm ci --prefix contracts --ignore-scripts
forge test --root contracts
FOUNDRY_PROFILE=formal /tmp/warrant-formal/bin/python scripts/run-halmos.py \
  --root contracts --forge-build-out out-formal --contract ProofInvoiceEscrowTest \
  --solver z3 --solver-timeout-assertion 30000 --json-output /tmp/symbolic.json
python3 scripts/test_proof_release.py
python3 scripts/proof-release.py prepare \
  --verus /path/to/verus --formal-python /tmp/warrant-formal/bin/python
```

Read the documented Halmos compatibility fix before trusting its result. The
upstream SHA-256 precompile model has a byte/bit sort mismatch; our wrapper fixes
only that sort in memory. It does not prove SHA-256 or the cryptographic verifier.

Generated keys, receipt directories, build outputs and caches are ignored and
were not pushed. The release manifest is an evidence record, not an attestation
from a trusted signer and not an end-to-end proof certificate.
