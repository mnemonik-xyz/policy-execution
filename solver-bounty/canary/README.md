# Canary evidence

This folder holds the deployment manifest and the evidence of each solver-bounty
canary run. Plan item 10 in `docs/product/mainnet-canary-plan.md` defines it.
`scripts/solver-canary.py evidence` writes the files. People do not write them by hand.

## Layout

```text
solver-bounty/canary/
  README.md               This file.
  manifest.schema.json    JSON Schema, draft 2020-12, for manifest.json.
  local/                  Anvil rehearsals. Chain ID 31337 only.
    <UTC timestamp>/      One run directory per rehearsal.
  mainnet/                Arc mainnet. Chain ID 5042 only.
    <UTC timestamp>/      One run directory per mainnet run.
```

A run directory is named by the UTC time of its `init` step, for example
`20261014T120000Z`. Pass that path as `--dir` to every step of the script.
The `init` step creates the directory.

Each run directory holds:

| File | Writer | Content |
|---|---|---|
| `manifest.json` | `evidence` | Deployment manifest. See the schema. |
| `result.json` | `evidence` | Settlement summary and the checks that passed. |
| `state.json` | every step | Script state. Signer labels, terms, transaction intents. |
| `instance.json` | `init` | The workload instance. |
| `check-*.json` | `check` | Chain ID, token decimals and balances at that time. |
| `deployment.json` | `deploy` | Foundry broadcast record of the two contract creations. |
| `rfq.json`, `quote.json`, `policy.json` | `quote` | The offered terms. |
| `delivery.json`, `schedule.json`, `native.json` | `deliver` | The seller's schedule and the native check. |
| `prove.log`, `evm.json` | `prove` | Prover output and the Groth16 seal with its journal. |
| `settlement.json` | `settle` | Receipt of the settlement transaction. |

The receipt files `solver.receipt` and `solver-groth16.receipt` are binary and
large. Keep them out of the commit when they exceed a few megabytes. `evm.json`
holds the seal that the chain verified.

## Rules

- `local/` and `mainnet/` never mix. The script refuses `evidence` when the
  chain ID in `state.json` does not match the folder, and refuses any other chain ID.
- Never commit a private key, a keystore file, an RPC URL or an environment dump.
  The script never reads a key. The RPC URL lives only in `WARRANT_CANARY_RPC`.
  Check `prove.log` for environment text before you commit it.
- The GHCR worker image digest is read at deploy time of the GPU run, not copied
  from documentation. Pass it with `--worker-digest sha256:...` when known.
- A run directory is never edited after `evidence` wrote `result.json`.
  The script refuses a second `evidence` call. A new attempt is a new run directory.
- A mainnet manifest must record a Docker builder image. A `local` builder is
  accepted only in `local/`.

## Manifest fields and their sources

| Field | Source | Status on 2026-10-14 |
|---|---|---|
| `source_commit`, `source_dirty` | `git rev-parse HEAD`, `git status --porcelain` at evidence time | Verified by the script. |
| `compiler` | `contracts/foundry.toml`: solc 0.8.28, Cancun, 200 optimizer runs | Verified in the file. |
| `dependencies.openzeppelin_contracts` | `contracts/package.json`: 5.6.1 | Verified in the file. |
| `dependencies.risc0_ethereum_contracts` | `contracts/vendor/risc0/PROVENANCE.json`: tag v3.0.0 | Verified in the file. |
| `dependencies.risc0_zkvm` | `Cargo.lock`: risc0-zkvm 3.0.5 | Verified in the file. |
| `chain_id`, `token`, `escrow`, `verifier`, `image_id`, `code_hashes`, `addresses` | `state.json`, written by `init` and `deploy` | Read from the chain at deploy time. |
| `image_builder` | `risc0-build` 3.0.5 default tag `r0.1.88.0`, or `RISC0_DOCKER_CONTAINER_TAG` | See the note below. |
| `transactions` | `state.json` and `deployment.json` | Hashes of broadcast steps only. |
| `worker_image` | `--worker-digest` argument | Optional. The GPU run is separate. |
| `created_at` | Clock of the machine that ran `evidence` | UTC. |

Note on `image_builder`. `methods/build.rs` does not set a builder tag. The
`risc0-build` 3.0.5 crate then uses `risczero/risc0-guest-builder:r0.1.88.0`,
unless `RISC0_DOCKER_CONTAINER_TAG` overrides it. The `deploy` step records the
tag from that rule at build time. The script does not ask Docker which image
digest it pulled. The field is a free string within the schema pattern.
Unverified on 2026-10-14: no Docker build of the guest has run on this branch yet,
so the recorded tag has not been compared with a pulled image.

The in-script check tests the required keys, the hex patterns, the fixed
compiler and dependency values, the chain-to-folder rule and the digest pattern.
It is not a full JSON Schema validator. A reader with `jsonschema` installed can
run the full check:

```sh
python3 -c 'import json, jsonschema; jsonschema.validate(
  json.load(open("solver-bounty/canary/local/RUN/manifest.json")),
  json.load(open("solver-bounty/canary/manifest.schema.json")))'
```

`scripts/check-arc-solver.py` reads `manifest.json` after deployment and
compares the recorded code hashes with the chain.
