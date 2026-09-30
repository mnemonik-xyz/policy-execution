# Third-party code: RISC Zero verifier contracts

Files under this directory are copied unchanged from
[risc0-ethereum](https://github.com/risc0/risc0-ethereum) at the commit recorded in
`PROVENANCE.json`. They are not part of the Licensed Work under the Mnemonik Shared
Revenue License 1.0 (clause 1.3) and remain under their own licenses:

| Files | License | Text |
|---|---|---|
| `contracts/src/groth16/Groth16Verifier.sol`, `contracts/src/groth16/RiscZeroGroth16Verifier.sol` | GPL-3.0 (generated with snarkJS) | `COPYING.GPL-3.0` |
| All other files | Apache-2.0 | `LICENSE` |

`LICENSE` is the upstream file and is hash-pinned in `PROVENANCE.json`; do not edit it.

Mnemonik contracts under `contracts/src/` do not import these files. They call a
separately deployed verifier through their own `IZkvmVerifier` interface. Keep it that
way: importing a GPL-3.0 file into `contracts/src/` would make the combined contract a
GPL-3.0 work. The deployment scripts and tests that do import the verifier are
themselves marked `GPL-3.0`.

Where RISC Zero's canonical verifier router is already deployed on the target chain,
prefer pointing the vault at that address over deploying this copy.
