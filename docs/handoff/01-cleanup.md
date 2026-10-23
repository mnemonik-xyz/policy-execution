# Cleanup that is not finished

State on 2026-10-23. Check the open pull requests first (see `README.md`).

## Already done

| Item | Where it went | Pull request |
|---|---|---|
| Showcases and the seller MCP | `warrant-showcases` | merged |
| Buyer wheel | `warrant-buyer` | merged |
| Ledger connector and `ids` helper | `warrant-bean-count-ledger` | `policy-execution#70` and `warrant-bean-count-ledger#1` merged |
| Canary operations | `warrant-canary-deployment` | `policy-execution#71` and `warrant-canary-deployment#1` merged. `#2` open |
| Swap crates and the DeFi swap doc | `warrant-swap` | `policy-execution#72` merged. `warrant-swap#1` still open |

## Remaining work

### C1. Remove the buyer-signed settlement path

Confirm decision 5 with the owner once (see `README.md`). Pull request #25 is merged and agrees with the removal.

Remove:

- `InvoiceEscrow.sol`: `settleSigned`, the `signer`, `signerAllowance`, `signerSpent`, `signerRevoked` and `proofThreshold` terms, `revokeSigner`, `signerDigest`, `SIGNER_TAG`, the `SignerRevoked` event and the related errors.
- `host/src/signer.rs` and the `invoice-sign` subcommand in `host/src/main.rs`. Remove its tests in `host/tests/tooling.rs`.
- `core/src/evidence.rs`: `signer_digest` and `sign_journal`. Keep everything else.
- `core/tests/invoice.rs` and `contracts/test/InvoiceEscrow.t.sol`: the tests of the signed path.
- `scripts/invoice-demo.py`: the signed flow and the `--signed-only` option.
- Docs: `invoice-escrow.md`, `README.md`, `validation-results.md`, `contracts/README.md`.

This changes the `offer` terms and the `Offered` event. The ABI changes. So:

1. The deployed `InvoiceEscrow` contracts are not compatible. Deploy new ones. Do not reuse old addresses.
2. `warrant-bean-count-ledger` decodes the events `Offered` and `SignerRevoked` with fixed topics. Update its topic table and its test.
3. `warrant-showcases` and `warrant-buyer` may read the old `offer` layout. Search them for `signer`.
4. The journal does not change. The guest image does not change. Check that `invoice-image-id` is the same before and after.
5. The Solidity test must still mutate all 15 journal words.

Acceptance:

- `cargo test -p warrant-policy --locked` passes.
- `forge test` passes. Run it on a machine with Foundry. The cloud session cannot.
- `python3 scripts/invoice-demo.py` runs the proof path and the buyer approval path.
- `rg -n 'settleSigned|invoice-sign|signer' contracts host core scripts` finds nothing outdated.
- The invoice image ID is unchanged.

### C2. Reorganize the documents (frozen until renames are allowed)

This step moves files. Do not start it while the rename freeze in C5 holds. Edits inside files are allowed.

Current problems: five documents sit in the repository root, the product specs are in `docs/product/`,
and `README.md` is a changelog with old numbers.

Target layout:

```text
README.md                    short: what it is, quick start, layout, index
docs/
  README.md                  index by topic
  framework.md               the design (exists)
  handoff/                   this folder
  architecture.md            from data-flow.md
  cases/
    task.md                  from task-escrow.md
    invoice.md               from invoice-escrow.md
    invoice-checker.md       from evidence-checker.md
    rent.md                  from docs/product/lease-evidence.md 
  validation/
    results.md               from validation-results.md
    evidence.json            from validation-evidence.json
```

Stay beside the code: `verified/README.md`, `verified/verification-results.md`,
`contracts/README.md`, `templates/README.md`, `solver-bounty/README.md`, `solver-bounty/validation.md`.

Move to the `warrant` repository (product and site specifications, not the proving path):
`docs/product/README.md`, `architecture-api.md`, `delivery-plan.md`, `execution-funding.md`,
`frontend.md`, `policy-catalog.md`, `settlement.md`.
Keep `lease-evidence.md` here as `docs/cases/rent.md`: it is the spec of the first target case.
`warrant-swap` already holds `defi-swaps.md`.

Steps:

1. Use `git mv`. Keep the history of the files that stay in this repository.
2. Fix every relative link. Write a small script that resolves each `](path)` link before and after the move.
   Check that every link target exists.
3. Fix the code comments that name a moved file (`core/src/evidence.rs`, for example, names `evidence-checker.md`).
4. Rewrite `README.md`. Remove the dated changelog sections. Put the history in `docs/history.md` if the owner wants it.
   Update or remove the old test counts. Regenerate the figures from a real run.
5. Remove the broken link to `../proof-execution/`.
6. Edit the files in Simplified Technical English as you touch them.

Acceptance: no broken relative link. `rg -n '\]\(' --glob '*.md'` shows only existing targets. The root holds no case document.

### C3. Remove leftovers

- `showcases/ionet_arc/__pycache__` exists on disk but not in git. Delete the folder.
- `validation-evidence.json` is in the root. Move it with C2.
- `scripts/solver-agent.py` stays. `solver-demo.py` uses it.

### C4. Bump the submodule pointers

- `warrant` pins `policy-execution` at `8826f94`. That is many commits behind `main`.
- `tameion` pins `warrant` at an old commit (`e70dadc` on 2026-10-23 for the `warrant` side).
- Bump `warrant` first. Then bump `tameion`. Commit subject form: `Bump warrant to <short-sha> (<what changed>)`.
- Do not run `git checkout` in a submodule with uncommitted changes. To move the pointer:
  `git -C warrant fetch origin main` and `git update-index --cacheinfo 160000,<full-sha>,warrant`.

### C5. Renames (frozen)

The owner said on 2026-10-23: do not rename anything for now.
This covers the `methods/` crate (a better name would be `guests`) and every file move in C2.
Do not rename or move a file until the owner lifts this rule.
Note for later: renaming `methods/` probably changes the guest image IDs, because Rust embeds source paths
in panic messages (not tested). If the owner approves it, do it in framework step 4, where the images change anyway.

### C6. Fix the pointer docs

After `warrant-swap#1`, `warrant-canary-deployment#1` and `warrant-bean-count-ledger#1` merge, check that every link
from this repository to them works.
