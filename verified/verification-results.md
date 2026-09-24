# Verification results

## 2026-09-23: invoice atoms and three-valued evaluation

Added `LineLabelsWithin`, `NoDeniedTerm` and `WithinPo` to the shared evaluator,
and `evaluate3`/`decide`, which return Allow, Deny or Ask over facts that may be
unknown (strong Kleene semantics). The evidence checker in `core/src/evidence.rs`
uses them; the existing `authorize()` path rejects the new atoms.

- Verus `0.2026.09.20.aef82ed`, with `--no-cheating`: **16 verified, 0 errors**.
  New obligations: `evaluate3 == kleene`, `decide` maps exactly onto `kleene`, and
  `decision_sound` (Allow holds, and Deny fails, for every completion of the
  unknown facts).
- **14 mutations rejected**: the original 8 plus line-label, denied-term and
  purchase-order bypasses, an ignored unknown conjunct, an ignored unknown line
  label, and Ask returned as Allow. Postcondition or loop-invariant failures both
  count; compiler errors do not.
- **40 native tests passed**: 17 existing, 20 invoice fixtures (E1–E13 plus
  authority, signature, path-separation and two Solidity-fixture checks) and 3
  XML reader tests. Solidity: 57 tests, 25 of them for `InvoiceEscrow`.
- Existing policy commitments are unchanged: `policy_hash` of both fixture
  policies is byte-identical to `main` (new `Rule` variants are appended).
- Shared evaluator source SHA-256:
  `f48d2294ace11540c1352b1da9a0a24a42f8c920b7d644e62b515e42cf966bd4`

**Not re-run:** the RISC Zero guest was not rebuilt (no `rzup` toolchain in this
environment). The guest's evaluator code changed, so its image ID changes on the
next build; the image ID and receipts recorded below belong to the 2026-09-22
snapshot and will not verify against a rebuilt guest.

## 2026-09-22

The first exercise verified the shared policy evaluator and integrated it into
both native authorization and the RISC Zero guest. It did not formally verify
the complete authorization pipeline or implement payment settlement. Ownership
and approval governance decisions remain open.

## Observed checks

- Verus `0.2026.09.20.aef82ed`, with `--no-cheating`: **7 verified, 0 errors**.
- **8 mutations rejected** through failed postconditions, not compiler errors.
- **24 tests passed**: 17 native, 4 guest execution and 3 receipt verification.
- Both new receipts are real succinct receipts, independently verified against
  the same image. Their journals match native evaluation.
- Modified journal words, a wrong image ID and an actual previous-image receipt
  were rejected. Both fixture policy commitments stayed unchanged after the
  evaluator refactor.
- Formatting, changed-document local links and tracked whitespace checks passed.

| Fixture | Local proving time | Segments | Receipt size |
| --- | --- | --- | --- |
| request | 223.98 s | 4 | 223,994 bytes |
| alternative | 235.68 s | 4 | 223,994 bytes |

Each run reported 1,048,576 total cycles, including segment padding, with four
Rayon workers and a segment limit of `2^18`. These are local observations on
synthetic fixtures, not performance guarantees. No funds moved.

## Build identity

Shared evaluator source SHA-256:

```text
539e977f1d24f0e13f929bb30e156a88d5167c69f5fd28a70ef84b8dabf928d5
```

Guest image ID, as eight decimal words emitted by the host:

```text
[766366895, 1124254008, 4187169255, 2988072234, 891370815, 2843878131, 4147897537, 1578163252]
```

Workspace and guest lockfile SHA-256 values, respectively:

```text
eb19a76810bb30aee05496578aacdd0e9ff56b80061f4a3a338008dcf0e17634
aa63afc1904d758559d51e625778d39a08b91cdd90f60f917ca6d23e490a3ff9
```

These identifiers describe the exercised snapshot. They are not a formal proof
that the compiler or build pipeline preserves semantics. See the
[verification scope and trusted dependencies](README.md#trust-boundary).

## Reproduce the receipt checks

After running the [formal verification command](README.md#reproduce), rebuild the
host and generate synthetic inputs and receipts in a fresh directory:

```sh
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
mkdir -p artifacts
export WARRANT_RECEIPT_DIR="$(mktemp -d "$PWD/artifacts/exercise.XXXXXX")"
cargo run --locked -q -p warrant-policy --example fixture > "$WARRANT_RECEIPT_DIR/request.json"
cargo run --locked -q -p warrant-policy --example fixture -- alternative > "$WARRANT_RECEIPT_DIR/alternative.json"
RAYON_NUM_THREADS=4 target/release/warrant-host prove "$WARRANT_RECEIPT_DIR/request.json" "$WARRANT_RECEIPT_DIR/request.receipt"
RAYON_NUM_THREADS=4 target/release/warrant-host prove "$WARRANT_RECEIPT_DIR/alternative.json" "$WARRANT_RECEIPT_DIR/alternative.receipt"
RISC0_BUILD_LOCKED=1 cargo test -p warrant-host --release --locked --test receipts -- --ignored
```

Run from `warrant/policy-execution`. The recorded local artifacts and detailed
logs are in ignored `artifacts/verus/`, including `verification-summary.json`.
Previously generated receipts remain in `artifacts/`; they belong to the old
image and are not substitutes for the fresh checks above.
