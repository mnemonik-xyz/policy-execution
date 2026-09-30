# Licensing

Unless a file states otherwise, this repository is licensed under the Mnemonik Shared
Revenue License 1.0 (`LICENSE`, SPDX `LicenseRef-Mnemonik-SRL-1.0`).

Exceptions:

- `contracts/vendor/risc0/` is third-party code under Apache-2.0 and GPL-3.0; see
  `contracts/vendor/risc0/NOTICE.md`.
- `contracts/script/Deploy.s.sol`, `contracts/script/DeployInvoice.s.sol`,
  `contracts/test/RealVerifier.t.sol` and `contracts/test/ArcProbe.t.sol` import the
  GPL-3.0 RISC Zero Groth16 verifier and are licensed GPL-3.0.
- Dependencies (Rust crates, OpenZeppelin, Foundry libraries) remain under their own
  licenses.
