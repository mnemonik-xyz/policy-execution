# Warrant payment contracts

- `src/ProofInvoiceEscrow.sol` and `src/ProofInvoiceFactory.sol`: strict proof-only
  settlement and replay protection shared by a factory's escrows. See the
  [assurance scope and candidate release workflow](../formal-payment-assurance.md).
  `WARRANT_PROOF_ONLY=true` selects a **new** factory in `DeployInvoice.s.sol`.
  To retain replay history when changing the guest, use the existing factory's
  `createEscrow(imageId)` instead. Customer approval of the image remains required.

- `src/PolicyExecutionVault.sol`: customer-controlled, revocable spending delegation.
- `src/TaskEscrow.sol`: funded fixed-price agreements, locked when the recipient accepts.
- `src/InvoiceEscrow.sol`: funded purchase orders settled by proven, signed or
  buyer-approved invoices up to a ceiling; see [the invoice escrow](../invoice-escrow.md).
  Deploy with `script/DeployInvoice.s.sol` and the `warrant-host invoice-image-id`
  output. The signer, its allowance and the proof threshold are per order, chosen
  by the buyer in `offer(Terms)`; nothing about them is fixed at deployment.

See [the escrow design and lifecycle](../task-escrow.md) for permissions, journeys,
policy binding, refund rules and security boundaries.

## Build and test

Requires Foundry and Node/npm. Dependencies pin OpenZeppelin Contracts 5.6.1;
Foundry pins Solidity 0.8.28 and targets Cancun.

```sh
npm ci --ignore-scripts
forge build
forge test
forge fmt --check src test script
```

State-machine tests use an explicit mock verifier. The real-verifier test always
runs using a checked-in synthetic proof fixture. Set `WARRANT_EVM_FIXTURE` to an
absolute path to a newly exported real receipt inside `../artifacts/` to override it. The
[local demo](../README.md#reproduce-deployment-and-real-settlement-locally) performs
real-proof escrow settlement. Never deploy the test verifier as a payment verifier.

Upstream verifier sources are vendored unchanged under `vendor/risc0`, with commit,
file hashes and license recorded in `PROVENANCE.json` and `LICENSE`. The Groth16
verifier files are GPL-3.0; see `vendor/risc0/NOTICE.md` before importing them. The
deployment script pins its control IDs and deploys the immutable base verifier directly.

## Deploy the verifier and escrow

Build the Rust host first and obtain its image ID:

```sh
# Run from policy-execution/
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
target/release/warrant-host image-id
```

From `contracts/`, set `WARRANT_DEPLOYER` to your signing account address, then
set the target RPC, token address and printed image ID. Use a
Foundry keystore account (`cast wallet import warrant-deployer --interactive`) or
supported hardware wallet; do not put private keys into source files.

```sh
export WARRANT_RPC_URL=https://rpc.testnet.arc.io
export WARRANT_TOKEN=0x3600000000000000000000000000000000000000
export WARRANT_IMAGE_ID="$(../target/release/warrant-host image-id)"
forge script script/Deploy.s.sol:Deploy --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --slow
# After reviewing the simulation, repeat with --broadcast to submit transactions.
```

`Deploy.s.sol` deploys the pinned base Groth16 verifier and then an escrow using
that address. Neither contract has an administrator or upgrade setter. Record
both addresses, chain ID, image ID and deployment transaction hashes from the
Foundry broadcast output. The token must already exist on the selected chain.
Before funding tasks, call the deployed verifier with a real exported receipt and
confirm that the deployed escrow reports the expected `token`, `verifier`, and
`imageId`.

Arc notes, checked 2026-09-23:

- Arc is not listed in the [RISC Zero deployment registry](https://dev.risczero.com/api/blockchain-integration/contracts/verifier).
  This procedure deploys a verifier; it does not assume an existing Arc address.
- [Arc Testnet](https://docs.arc.io/arc/references/connect-to-arc) uses chain ID
  5042002. Check `cast chain-id --rpc-url "$WARRANT_RPC_URL"` before broadcasting.
- [Arc USDC](https://docs.arc.io/arc/references/contract-addresses) exposes an ERC-20
  interface at the address above with six decimals. Use ERC-20 units throughout
  Warrant; native gas balances use a different precision.
- Arc Testnet read-only simulation passes: real proof verification, rejection of a
  changed journal, six-decimal USDC access, and escrow construction. Python HTTP
  requests initially returned 403; Foundry RPC calls succeeded. Reproduce without
  broadcasting from the repository root:

  ```sh
  python3 scripts/check-arc.py contracts/test/fixtures/risc0-3.0.5.json
  ```

  The proof fixture has a synthetic local payment domain. This checks verifier
  compatibility, not authorization to pay that task on Arc. No public-network
  deployment or funded Arc settlement has been performed.
- Arc's [EVM differences](https://docs.arc.io/arc/references/evm-differences)
  include a minimum base fee. Obtain current fee estimates from the RPC before
  broadcasting; an insufficient maximum fee can leave transactions unincluded.
