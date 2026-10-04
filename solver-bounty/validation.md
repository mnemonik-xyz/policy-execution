# Solver bounty validation

Date: 2026-10-04. Base: policy-execution `0b84232d122ba3b9d587d32f8e46afe758d7e56d`.
This base is the implementation pinned by Tameion's Warrant submodule.
The implementation PR targets `invoice-authentication` to keep invoice changes separate.

## Completed local checks

| Check | Observed result |
|---|---|
| Native Rust tests | 65 passed, including 17 solver tests |
| Solidity regression suite | 74 passed, including 12 solver escrow tests |
| Result mutation fuzz test | 256 cases passed |
| Capacity checker | 375 small schedules agreed with an independent tick oracle |
| Native demonstration | Quote, schedule, policy authorization, and canonical result passed |
| Mock settlement demonstration | Buyer cancellation rejected; seller paid; result published; substitution and replay rejected |
| Solver guest build | Compiled with the pinned RISC Zero Rust 1.88.0 toolchain |
| Solver guest execution | 59,446 cycles; journal matched native authorization |
| Guest regression tests | 2 passed, including invalid schedule and instance rejection |
| Real receipt mutation test | Passed; all 13 journal words and wrong image rejected |

The native compiler is Rust 1.98.1. Native builds produced undefined-symbol errors
with Rust 1.98.1 and 1.99.0 in this environment.
The tested fix disables native ThinLTO and uses one codegen unit for the Verus
procedural macro. The workspace pins the documented 1.98.1 version.
Each guest retains its separate release profile.
Contracts used Foundry 1.8.4 and Solidity 0.8.28.

The example instance contains three synthetic jobs. Its computed cost is 14.
The mock demonstration pays 10,000,000 test token base units.
It records `realProof: false` and never claims cryptographic settlement.

## Proof validation

A fresh real succinct receipt was generated and verified locally.
The run used one segment and 131,072 padded prover cycles.
It took 135.29 seconds with four Rayon workers. The receipt contains 224,058 bytes.
This is one sample, not a benchmark distribution.
The separate `verify` command also accepted the receipt.
The guest regression tests and real receipt mutation test also passed.

The workspace has no Docker daemon. Local Groth16 wrapping and real solver
settlement have not been demonstrated here.
The complete flow passed on a GitHub Actions runner with Docker, as recorded below.

The existing invoice and task proof fixtures pass the contract regression suite.
They do not establish solver proof settlement.

## Real solver settlement in CI

[Run 37177967347](https://github.com/mnemonik-xyz/policy-execution/actions/runs/37177967347)
passed on 2026-10-04 at commit `476063dee1e872e1706e23258f5b3ccb53740a72`.
It generated a fresh succinct receipt, wrapped it as Groth16, and settled through
the real verifier on a private Anvil chain.

- The seller received 10,000,000 test token base units.
- The escrow published the exact schedule, with computed cost 14.
- The buyer sent no transaction after acceptance.
- Buyer cancellation, result substitution, and replay failed as expected.
- The run recorded `realProof: true`, `mockVerifier: false`, and `chainSettlement: true`.

The succinct proof took 147.57 seconds in this sample.
This excludes Groth16 wrapping and setup. It is not a benchmark distribution.

The [evidence archive](https://github.com/mnemonik-xyz/policy-execution/actions/runs/37177967347/artifacts/11294136738)
contains both receipts, deployment data, the EVM journal, and the settlement receipt.
Its SHA-256 is `0834d6fa710d7d0cb1afe1bc9a549e1b52124c4dbc56db1cbee85246faec873e`.
The downloaded archive matched that digest.
The [saved result](ci-result.json) preserves the run's summary.

This was a local test chain with a synthetic workload and test funds.
It was not a public deployment or a customer transaction.

## Reproduce

```sh
cargo test --locked -p warrant-policy -p warrant-solver
forge test --root contracts
cargo test --release --locked -p warrant-host --test solver_execution
python3 scripts/solver-demo.py --native-only
python3 scripts/solver-demo.py --mock-settlement
python3 scripts/solver-demo.py
```

To test a fresh solver receipt, set `WARRANT_SOLVER_RECEIPT_DIR` to the demo directory:

```sh
cargo test --release --locked -p warrant-host --test solver_receipt -- --ignored
```

That test verifies the receipt and rejects a changed image or any changed journal word.
It requires matching `input.json` and `solver.receipt`; it does not create either file.

The shared Verus evaluator source is unchanged. Verus was not rerun.
The new checker and contracts remain outside the evaluator's formal proof.
No production audit, public deployment, or real customer transaction is claimed.
