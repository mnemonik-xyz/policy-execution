# Warrant implementation validation

## Multi-currency invoices — 2026-10-08

Checker version 3 ([specification](../multi-currency/spec.md),
[evidence checker §4a](evidence-checker.md)). Measured on Linux x86-64 in a cloud
session, without the RISC Zero toolchain.

| Check | Result |
|---|---|
| `cargo test -p warrant-policy --locked` | 76 passed: 39 invoice (11 new), 17 policy, 17 solver, 3 unit |
| `cargo test -p warrant-ids --locked` | 4 passed; test vectors regenerated |
| `forge test` | 74 passed, using regenerated `invoice-journal.json` and `invoice-signature.json` |
| `connectors/ledger` pytest | 14 passed |
| `scripts/invoice-demo.py --signed-only --ledger` on Anvil | Signed and buyer-approved USD settlements, then the ledger; `bean-check` passed |

New tests cover: an EUR invoice at the signed rate (E11, now `Allow`), an AMD
invoice rounded down, the unchanged USD amount, a USD order with another rate, a
zero rate, a currency outside the policy, an unsupported currency, `1.005` and
`6.000000` USD, an amount in another currency, conversion overflow, and policy
currency validation.

Not done in this session:

- The invoice guest was not rebuilt. `rzup` could not download the toolchain
  through the session proxy. The new image ID is therefore not recorded.
- No real proof and no Groth16 wrap for checker version 3. `invoice-proof.json`
  belongs to the version-2 image.
- The demo ran with `RISC0_SKIP_BUILD=1` and a stand-in image ID. That is
  allowed only with `--signed-only`, whose paths never verify a proof.
- No EUR or AMD settlement on chain. The unit tests cover the conversion; the
  demo order is in USD.
- Verus was not rerun. The evaluator source is unchanged.

## Invoice authentication and customer isolation — 2026-09-30

This working revision changes invoice policy commitments and settlement encoding.
It is not compatible with existing invoice receipts or an older invoice escrow.
The task vault and task escrow authorization formats are unchanged.

### Changes and regressions

- Order IDs now include the funding customer (`msg.sender` at offer time).
- Invoice policies require a nonzero `customer` and a valid `invoice_key`, and use commitment domain
  `warrant/invoice-policy/v2`. The invoice journal is 15 ABI words, appending
  customer after `poMaxTotal`.
- Replay state is indexed by customer and obligation ID, across that customer's
  orders and all three authenticators. Another buyer's arbitrary approval cannot
  block payment. This is not company-wide or cross-deployment deduplication.
- The two attack regression tests failed on the old code (`AlreadyPaid` and
  `InvalidState`) and pass after the fix. Additional tests bind the customer to
  the signature/proof journal, reject legacy journals, retain cross-policy replay
  rejection for the same customer and exercise unknown-fact semantics.

- Mandatory invoice-source attestations bind exact document bytes, customer,
  PO, payment scope and validity. They are checked before the rule tree, so
  `Any` cannot bypass authentication. Missing evidence asks; mismatched or forged
  evidence rejects. Tests reproduce the old renumbering and consistent-amount
  attacks and confirm rejection without a fresh source signature.
- Issuer tooling prepares statements from exact XML bytes and signs them under
  `warrant/invoice-attestation/v1`; both commands refuse output overwrite.
  The checker version is now 2.

### Observed validation

- **62 Rust tests passed:** 48 native policy/XML/invoice, 3 signer, 3 template,
  2 issuer CLI and 6 guest execution tests. **3 receipt tests skipped** because
  they require separate real receipts. No skipped test is counted as passing.
- **62 Solidity tests passed**, including 29 invoice escrow tests and a current
  real invoice proof settlement test. Each of the three fuzz tests ran 256 cases.
  The current proof rejects mutations to all 15 journal words and rejects replay
  after settlement. The older task receipt remains a separate verifier fixture.
- Rebuilt invoice guest execution agrees with native authorization for two customers.
- `python3 scripts/invoice-demo.py` deployed fresh local contracts, funded and
  accepted an order, and settled by signature, buyer approval and real proof.
  Replay, invoice tampering, smuggled spend inputs and an unnamed signing key
  were rejected; missing invoice attestation produced Ask. Synthetic Anvil funds
  only: 1,320 + 330 + 1,320 paid from a 3,000 ceiling, leaving 30.
- One sample: signed flow **0.04 s**, succinct proving **445.65 s**, Groth16
  wrapping **79.73 s**. The guest used **2,097,152 cycles / eight segments**;
  receipts were **224,186 / 1,457 bytes**, respectively. Host: macOS ARM64,
  14 reported CPUs, four Rayon workers, segment exponent 18. Wrapping used
  an x86 Docker image under emulation. These are not repeated benchmarks;
  peak memory and reproducible Docker guest compilation were not validated.
- The Verus evaluator source is unchanged; the prior 16-obligation result is
  historical evidence, not a fresh proof of the modified authorization pipeline.

Commands (from `policy-execution/`):

```sh
RISC0_BUILD_LOCKED=1 cargo test -p warrant-policy -p warrant-host --locked -- --test-threads=1
forge test --offline --root contracts
python3 scripts/invoice-demo.py
```

Guest execution and Anvil need permission to start local processes/listeners.
The run summary and artifact hashes are recorded under `invoiceAuthentication20260930`
in [validation-evidence.json](validation-evidence.json); the earlier customer-isolation-only and original task-proof
records are retained separately. Local generated artifacts are ignored by Git.

### Deployment and trust boundary

A local guest build is not the Docker-based reproducible build. Deploy a new
escrow with the reviewed current image and approve newly computed invoice
policy commitments; old invoice approvals and receipts are incompatible.
No public-network deployment or production audit is part of this local check.

The buyer must choose and operate the invoice authority whose key the policy
pins. That authority must obtain or issue invoices independently of the agent.
Its signature prevents agent edits; it does not prove delivery, fair pricing or
that an authority has not endorsed the same debt under multiple numbers. Buyer
approval remains an explicit override. Source trust and debt deduplication are
operational assumptions, not undecided protocol behavior.

## Historical task prototype — 2026-09-23

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
