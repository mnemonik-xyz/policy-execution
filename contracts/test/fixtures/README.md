# Real verifier fixture

`risc0-3.0.5.json` is an actual Groth16 receipt export, generated from the synthetic
`core/examples/fixture.rs` request with public test issuer keys. It contains only
public journal data, seal, image ID and journal digest. It is not a fake receipt.
The synthetic domain is chain 31337, vault `0x0303…0303`, token `0x0404…0404`.

Generation uses `warrant-host prove`, `wrap`, then `export-evm`. The interpreter
image is `0xafd4ad2d38c10243e72193f92a5d1ab23f3d2135f32282a9c1e43bf734dc105e`.
`RealVerifierTest` validates it against the pinned upstream Solidity verifier and
rejects tampering. It is a verifier regression fixture, not proof that an arbitrary
later guest build is correct. Run `scripts/local-demo.py` to generate a fresh
proof and settle an accepted task against the current build.

## Authenticated invoice proof

`invoice-proof.json` is a real Groth16 export from the full invoice demo on
2026-09-30, using synthetic invoices and public test issuer keys. It records the
invoice guest image, input hash and relevant source hashes. Its journal has
15 words, including the funding customer.

`RealInvoiceSettlementTest` verifies this proof, rejects changes to each journal
word, reconstructs its local domain with Foundry cheatcodes, then funds, accepts
and settles through the actual escrow and verifier. A second settlement rejects.
The demo separately performed those operations on freshly deployed Anvil contracts.
It belongs to the checker-version-2 image. The 2026-10-08 multi-currency revision
changes the invoice image, so this proof remains a regression fixture for the old
image only. `invoice-journal.json` and `invoice-signature.json` were regenerated
for checker version 3 with `WARRANT_UPDATE_FIXTURES=1`.
Regenerate using `python3 scripts/invoice-demo.py` after guest changes; an old
receipt does not validate changed guest code. See [validation results](../../../validation-results.md)
for the distinction between cryptographic checks and evaluator proofs.
