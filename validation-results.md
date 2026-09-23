# Warrant implementation validation

Verified 2026-09-23. These results describe a CLI and smart-contract prototype,
not an audited production deployment or a hosted marketplace.

## Requirements and evidence

| Requirement | Implementation and verification |
| --- | --- |
| Reusable policies | Two JSON templates, strict parameter instantiation and independently reproducible policy hash; three template tests |
| Authenticated evidence | Registry/reviewer signing CLI; signature and request binding tests, including a CLI-generated signature round trip |
| Fixed interpreter | Shared Rust evaluator used natively and inside the zkVM; guest/native agreement and rejection tests |
| Formal evaluator correctness | Verus: 7 verified, 0 errors; all 8 executable mutations rejected |
| Customer spending authorization | Vault owner approves policy/version and budget; administrative and payment tests |
| Committed task agreement | Escrow reserves full funding, records immutable terms and requires recipient acceptance |
| Agent payment protection | No post-acceptance cancellation or withdrawal; exact payout or agreed timeout refund |
| Real proof enforcement | Pinned upstream Groth16 verifier accepts an actual receipt and rejects tampering |
| Real escrow payment | Fresh local chain deployment, template commitment, funding, acceptance, proving, wrapping, settlement, balance checks and replay rejection |
| Deployment procedure | Foundry deployment script exercised locally; commands and Arc instructions in contracts README |
| Arc compatibility | Read-only Arc Testnet constructor simulation verifies a real proof, rejects a changed journal, reads USDC and constructs escrow |

## Test results

- Rust: **28 passing tests** — 17 native policy, 4 guest execution, 3 real receipt,
  3 template, and 1 issuer CLI round-trip test.
- Solidity: **32 passing tests**, no skipped tests. This includes 256 fuzz cases
  within the vault amount test, real proof verification, all twelve journal-word
  mutations, escrow lifecycle, authorization, reservation isolation, rollback,
  fee-reduced funding rejection and reentrant settlement rejection.
- Separate historical `../proof-execution` Circom prototype: **24 passing tests**.
- Formal evaluator: **7 verified, 0 errors**; **8/8** deliberate mutations rejected.
- Formatting, vendored-source SHA-256 checks and whitespace checks pass.

The contract fixture contains an actual proof; the unit mock is confined to
state-machine tests. The complete local demo generates a fresh proof and pays
through the real verifier. `execute` alone is never counted as proof generation.

## Reproducibility and proof identity

Run the commands in the [main README](README.md#complete-validation). The demo
writes its inputs, receipts, EVM export, deployment record and `result.json` to
`artifacts/escrow-<timestamp>/`; generated artifacts are ignored by Git. The
checked-in public verifier fixture is under `contracts/test/fixtures/`.

- Interpreter image:
  `0xafd4ad2d38c10243e72193f92a5d1ab23f3d2135f32282a9c1e43bf734dc105e`
- Verified evaluator source SHA-256:
  `539e977f1d24f0e13f929bb30e156a88d5167c69f5fd28a70ef84b8dabf928d5`
- Solidity verifier upstream commit:
  `32aa0b6f23ddd02dd93fc71717667606e5c7db86` (`v3.0.0`).
  Individual hashes and retained license are in `contracts/vendor/risc0/`.
- RISC Zero crates: `3.0.5`; Solidity: `0.8.28`; OpenZeppelin: `5.6.1`.

## What remains outside the guarantee

The proof establishes policy compliance with authenticated evidence, not objective
work quality or faithful translation of natural-language intent. A reviewer can
withhold evidence. Settlement must arrive before the agreed deadline. Provers see
their witnesses; public outputs are not anonymous. Policy files must be shared
with the agent; no hosted discovery or availability service is supplied.

The formal proof covers the evaluator only, not parsing, signature libraries,
serialization, the host, Solidity or the compiler chain. Customer policies,
issuer keys and immutable deployment parameters still require review.

Arc validation used `eth_call`, with no transaction broadcast or persistent
contract creation. Its proof checks cryptographic compatibility; the synthetic
fixture's local payment domain is not an authorization to pay on Arc. Public
Arc deployment, funded Arc settlement, a production audit and managed services
have not been performed and are not claimed by these results.
