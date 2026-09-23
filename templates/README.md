# Policy templates

Templates are reviewed JSON rule trees with explicit `{"$param":"name"}`
substitutions. They are off-chain source material, not authority to spend funds.
The customer approves the resulting concrete policy hash in a vault or task escrow.
The agent must independently reproduce that hash before accepting a task.

- `accepted-contractor-v1.json`: signed reviewer acceptance, category membership,
  and an amount cap.
- `accepted-deliverable-v1.json`: additionally binds the recipient and deliverable hash.
- `example-parameters.json`: synthetic local parameters with public test authorities,
  chain 31337 and dummy addresses/times. Never use these authority keys in production.

From the repository root:

```sh
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
target/release/warrant-policy instantiate templates/accepted-contractor-v1.json \
  templates/example-parameters.json artifacts/policy.json
target/release/warrant-policy hash artifacts/policy.json
```

Create `artifacts/` first if necessary. Instantiation refuses to overwrite output.
Both commands print the same commitment. Template IDs and descriptions are metadata;
only the concrete typed policy is committed. Changing template contents is visible
through its resulting rules/hash, not automatically trusted because its ID matches.

Parameters have exactly two objects: `policy` contains version, scope, validity and
compressed secp256k1 authority keys; `bindings` contains template parameters.
Unknown policy fields, missing/unused bindings, rule overrides and invalid typed
policies are rejected. Amounts are integers in token base units. Addresses/hashes
use arrays of 20/32 bytes, matching the Rust input schema. Additional deliverable
parameters are `recipient` and `deliverable_hash`.

Share the resulting policy JSON with the agent alongside the task ID. This local
file workflow implements policy distribution without requiring a marketplace or
registry. Hosted discovery, content availability and a template-review UI remain
outside this prototype.

## Registry and reviewer signing

The registry and reviewer run these commands separately, using their own secret
keys. Supply a file containing the 32-byte secp256k1 key encoded as hex, stored
outside this repository with restricted access. The printed public key is the
compressed byte array used in policy parameters.

```sh
target/release/warrant-evidence public-key /secure/registry.key
target/release/warrant-evidence sign-vendor /secure/registry.key \
  vendor.json vendor-signature.json
target/release/warrant-evidence sign-acceptance /secure/reviewer.key \
  acceptance.json acceptance-signature.json
```

Statements follow `VendorCredential` and `Acceptance` in `core/src/lib.rs`.
The registry signs scope, recipient, category and validity. The reviewer signs
scope, task ID, deliverable hash, recipient, amount, accepted flag and validity.
Sign only after checking those fields and the actual evidence; this utility is
not a reviewer or an automatic quality assessment.

The agent assembles `Input` JSON with `policy`, `request`, and `evidence`.
`evidence` has four fields: `vendor`, `vendor_signature`, `acceptance`, and
`acceptance_signature`. Insert the statement objects and signature arrays
produced above. `request` contains scope, task ID, deliverable hash, recipient and
amount. Run `warrant-host evaluate input.json` before proving. Signing keys remain
with the issuers; only signed statements go to the agent and prover.
