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
