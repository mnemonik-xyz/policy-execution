# Canary self-review of the solver settlement path

Status: self-review, 2026-10-14, commit `47b05c4` on branch `canary/prep`. Reviewer: the core team. No code changed.

This document is canary plan item 9 in [mainnet-canary-plan.md](../../docs/product/mainnet-canary-plan.md). It does not satisfy gate CHAIN-5 in [settlement.md](../../docs/product/settlement.md). CHAIN-5 requires an independent review. This is a review by the authors. The exception is recorded in `settlement.md`, section "Recorded exception for the Arc mainnet canary". The exception covers one capped order on the solver path only.

Scope: the code that the Arc mainnet canary exercises. The five areas are the checker, the projection into the journal, the escrow contracts, the proving build and the signing boundary. Each finding names the file and function that I read. Line numbers refer to `47b05c4`. For `scripts/solver-canary.py` only function names are given, because that file changed in the working tree during this review (manifest and evidence-folder code added after `cmd_settle`); the functions reviewed here are unchanged in that diff. A finding is "Resolved" when the code or a test already covers it. A finding is "Open" when a person must still act before or during the canary. Statements that I could not verify in the code carry the date 2026-10-14 and the word "unverified".

Severity scale: high, medium, low, note. "Note" records a verified property with no action needed.

## 1. The checker

Files: `core/src/solver.rs`, `core/src/lib.rs`, `core/tests/solver.rs`.

### S1.1 Cost sums and interval ends use checked arithmetic

Severity: note

Evidence: `check_schedule` in `core/src/solver.rs` lines 192 to 226. The end time uses `checked_add`. The job cost uses `checked_mul` twice. The total uses `checked_add`. The occupied-units sum uses `checked_add`. Every overflow returns `ArithmeticOverflow`. The test `rejects_overflow_in_end_cost_and_total` in `core/tests/solver.rs` line 203 covers the end time, a single job cost and the running total.

Resolution: no action. The arithmetic cannot wrap.

### S1.2 Duplicate, missing, reordered and extra assignments are rejected

Severity: note

Evidence: `check_schedule` line 175 requires one assignment per job. Line 181 requires `assignment.job == index`. `result_bytes` line 130 repeats the same check before encoding. These two rules make the schedule canonical. A duplicate job index, a missing job, a swapped pair or an extra entry each fail. The test `missing_duplicate_reordered_and_extra_jobs_are_rejected` at line 24 covers all four cases.

Resolution: no action. `result_bytes` cannot depend on input order, because only one order is accepted.

### S1.3 Every instance field is bound into the instance hash and the policy hash

Severity: note

Evidence: `instance_hash` line 113 hashes the whole `Instance` struct with `hash_tagged`, which uses bincode over typed structs (`core/src/lib.rs` line 123). `Machine`, `Job`, `Instance`, `Assignment`, `SolverPolicy`, `SolverInput` and `Request` all carry `deny_unknown_fields`. Extra JSON fields fail to parse instead of being dropped. `policy_hash` line 117 binds version, scope, validity window, checker version, instance hash, cost ceiling and rule. `authorize_solver` line 269 compares the recomputed instance hash with the policy. The test `checks_exact_instance_and_exact_result` at line 52 changes one price and expects `InstanceMismatch`.

Resolution: no action.

### S1.4 The rule tree adds no restriction that the escrow does not already enforce

Severity: low

Evidence: `supported_rule` line 234 accepts `All`, `Any`, `Accepted`, `AmountAtMost`, `DeliverableEquals` and `RecipientEquals`. `authorize_solver` line 279 sets `accepted: true` as a constant fact. So `Accepted` is always true in this path, and a rule `Any([Accepted, ...])` is always true. The mandatory gates at lines 268 to 278 stay outside the rule tree: instance match, schedule validity, cost ceiling and result hash. The escrow fixes recipient and amount from the offer terms (`TaskEscrow._settle` line 137). The test `permissive_policy_cannot_bypass_the_result_checker` at line 118 covers the gates.

Resolution: the canary policy uses `All([Accepted, AmountAtMost(amount), RecipientEquals(seller)])`, written by `cmd_quote`. A buyer must not expect a solver policy rule to restrict anything beyond the escrow terms. No code change for the canary.

### S1.5 The u64 request amount is a token amount; max_cost is a model unit

Severity: note

Evidence: `Request.amount` is `u64` (`core/src/lib.rs` line 69). `Task.amount` is `uint64` (`TaskEscrow.sol` line 27). Arc USDC has six decimals, so the 10 USDC cap is 10,000,000 units and fits. `SolverPolicy.max_cost` line 59 and `total_cost` are model units from `price_per_unit_tick`. The checker compares `total_cost` with `max_cost` at line 273. It never compares cost with the token amount. The seller receives the fixed amount when the proof is valid, whatever the modelled cost.

Resolution: no action. The evidence summary must keep the sentence that the checker proves a modelled cost, not a bill (`cmd_evidence` does).

### S1.6 Validity window is not checked inside the guest

Severity: note

Evidence: `authorize_solver` line 250 only checks `valid_after <= valid_until`. The guest has no clock. `TaskEscrow._settle` line 140 checks `block.timestamp` against both bounds and against `settleBy`.

Resolution: no action. This is the intended split.

## 2. The projection into the journal

Files: `methods/solver-guest/src/main.rs`, `host/src/main.rs`, `solver-cli/src/main.rs` (binary `warrant-solver`), `core/src/lib.rs`.

### S2.1 Journal layout matches the Solidity decoder word for word

Severity: note

Evidence: `Authorization::journal` in `core/src/lib.rs` line 305 writes 12 static words, big-endian integers padded to 32 bytes and addresses padded to 20 bytes. `SolverAuthorization::journal` in `core/src/solver.rs` line 81 appends one word with `total_cost`. `SolverBountyEscrow.settleWithResult` line 29 requires 416 bytes and decodes `(Authorization, uint64)`. Two cross-checks exist: `solidity_fixture_matches_native_journal_and_result` (`core/tests/solver.rs` line 344) and `testRustJournalAndCanonicalScheduleDecodeExactly` (`SolverBountyEscrow.t.sol` line 205) read the same fixture `contracts/test/fixtures/solver-journal.json`. The guest commits exactly `authorization.journal()` (`methods/solver-guest/src/main.rs` line 8).

Resolution: no action.

### S2.2 The contract does not compare totalCost with anything

Severity: low

Evidence: `settleWithResult` decodes `totalCost` and emits it in `ResultPublished` (line 38). No contract check uses it. The cost ceiling is enforced inside the proof: `authorize_solver` line 273, with `max_cost` bound into `policy_hash`, which the escrow compares with the offered `policyHash`.

Resolution: by design. The emitted value is evidence only. The buyer's guarantee comes from `max_cost` in the hashed policy. No action.

### S2.3 evidenceHash is only checked for non-zero on chain

Severity: note

Evidence: `_settle` line 139 requires `evidenceHash != 0`. The solver `evidence_hash` (`solver.rs` line 299) hashes checker version, instance hash, deliverable hash and total cost under a tag. The escrow cannot interpret it. The proof already binds it.

Resolution: no action.

### S2.4 Host selects the image by journal length; the canary compares it with the deployment

Severity: low

Evidence: `image_for` in `host/src/main.rs` line 20 maps 416 bytes to `WARRANT_SOLVER_GUEST_ID`. `wrap` and `export-evm` verify the receipt against that embedded image (lines 328 and 336). `export-evm` writes the embedded image ID, not one read from the receipt. If the host binary is rebuilt with a different toolchain between `deploy` and `prove`, the exported image ID changes. `cmd_prove` compares it with `state.image_id` and stops before any broadcast. `cmd_prove` itself does not require `RISC0_USE_DOCKER=1`.

Resolution: the check exists and runs before `settle`. Keep `RISC0_USE_DOCKER=1` exported for the whole session, as the README shows, so `prove` builds the same guest as `deploy`.

### S2.5 Native path and proven path run the same function and are compared

Severity: note

Evidence: `warrant-solver prepare` and `inspect` call `authorize_solver` (`solver-cli/src/main.rs` lines 61 and 68). `warrant-host solver-prove` calls the same function, then requires the receipt journal to equal it (line 150). `cmd_prove` compares the exported journal with `native.json`. The guest panics on denial (`expect` at `main.rs` line 7), so no receipt exists for a denied input.

Resolution: no action.

### S2.6 Result hash binding uses SHA-256 on both sides

Severity: note

Evidence: `result_hash` line 140 is SHA-256 of `result_bytes`. `prepare` sets `request.deliverable_hash` from it (`solver-cli` line 59). The escrow computes `sha256(result)` (`SolverBountyEscrow.sol` line 32). The minimum result length 39 equals tag 19 plus count 4 plus one 16-byte assignment. The maximum 2071 equals `MAX_RESULT_BYTES`; the test `canonical_result_encoding_matches_published_format` line 330 asserts 2071.

Resolution: no action.

## 3. SolverBountyEscrow and TaskEscrow

Files: `contracts/src/SolverBountyEscrow.sol`, `contracts/src/TaskEscrow.sol`, `contracts/test/SolverBountyEscrow.t.sol`, `contracts/test/TaskEscrow.t.sol`, `contracts/test/EscrowToken.t.sol`.

### S3.1 The hash-only settle entry is disabled

Severity: note

Evidence: `SolverBountyEscrow.settle` line 19 is `external pure override` and reverts `ResultRequired`. `testInheritedHashOnlyPathAlwaysReverts` line 97 calls it through both the derived and the `TaskEscrow` ABI.

Resolution: no action.

### S3.2 Result bytes are bound to the proven deliverable hash

Severity: note

Evidence: line 32 requires `39 <= result.length <= MAX_RESULT_BYTES` and `sha256(result) == a.deliverableHash` before `_settle`. The guest derived that hash from canonical bytes, so the only accepted preimage is the canonical schedule. Tests: `testWrongMissingOversizedOrUnprovedResultCannotPay` line 107 and the fuzz test `testFuzzAnyMutatedResultByteFails` line 195 (256 runs).

Resolution: no action.

### S3.3 Replay is impossible after payment

Severity: note

Evidence: `_settle` line 135 requires state `Accepted`; line 145 sets `Paid` before the transfer. `testReplayAndLegacyJournalsRejected` line 168 settles once and expects the second call to revert. `cmd_settle` in the canary repeats this check on chain with `eth_call` at the end of `cmd_settle`.

Resolution: no action.

### S3.4 Reentrancy through the token cannot pay twice

Severity: note

Evidence: `settleWithResult` carries `nonReentrant` (line 27). `_settle` updates state and `totalReserved` before `safeTransfer` (lines 145 to 147). `testTokenCannotReenterResultSettlement` line 188 uses `CallbackToken`, which calls back into the escrow during `transfer`; the callback fails and the seller receives one payment.

Resolution: no action.

### S3.5 Refund windows at, before and after both deadlines (CHAIN-3)

Severity: note

Evidence: `refund` line 108. Offered: customer any time; anyone strictly after `acceptBy`. Accepted: anyone strictly after `settleBy`. `_settle` allows settlement at exactly `settleBy`. Tests in `TaskEscrow.t.sol`: `testAcceptanceAndSettlementDeadlineInclusive` (warp 1200 and 2000), `testUnacceptedOfferTimeout` (1201), `testAcceptedTimeoutRejectsEvenValidProof` (2001), `testOfferCancellationAndTerminalId` (cancel then accept fails), `testAcceptedAgreementCannotBeCancelledOrReplaced` (accept then cancel fails). The two race orders are both covered. `SolverBountyEscrow.t.sol` repeats the deadline cases for the result path.

Resolution: no action. The canary records the live cancellation check in `cmd_accept`.

### S3.6 The relayer cannot change recipient or amount

Severity: note

Evidence: `_settle` line 137 compares `a.recipient` and `a.amount` with the stored task, and `a.chainId`, `a.vault`, `a.token`, `a.policyHash` and `a.policyVersion` with the deployment and the offer. The relayer only supplies calldata. `testLiveTermsStillRejectAnApprovedWrongRecipient` line 133 and `testEveryJournalWordIncludingComputedCostIsBound` line 122 cover every word.

Resolution: no action.

### S3.7 totalReserved is informational

Severity: note

Evidence: `offer` line 90 adds before `safeTransferFrom`; a failed transfer reverts the whole call (`testUnfundedOfferRevertsAtomically`). `refund` and `_settle` subtract. No function reads `totalReserved` to gate anything, and there is no sweep or admin function that could spend unreserved balance. `offer` line 93 rejects fee-on-transfer funding (`testFeeFundingCannotBorrowOtherReservation` in `EscrowToken.t.sol`).

Resolution: no action. The canary asserts `totalReserved == 0` after settlement in `cmd_settle`.

### S3.8 A token that returns false or reverts cannot leave the task half paid

Severity: low

Evidence: `SafeERC20.safeTransfer` reverts on `false` or revert. `testTransferFailureRollsBackPaymentAndPublication` line 178 shows state and reservation unchanged after a failed transfer, and a later success. If Arc USDC rejects the seller address, settlement reverts until `settleBy`, then anyone can refund the buyer.

Open item: whether the Arc mainnet USDC contract at `0x3600000000000000000000000000000000000000` has code, returns six decimals, returns `bool` on transfer and has no fee is unverified in this review (2026-10-14). The read-only probe of canary plan item 4 must establish it before `offer`. The `TaskEscrow` constructor reverts when the token address has no code (line 60), so a wrong address fails at deployment.

### S3.9 Event contents

Severity: note

Evidence: `Offered` carries every term. `Accepted` and `Refunded` carry the task ID. `Paid` carries task ID, deliverable hash and evidence hash. `ResultPublished` carries task ID, result hash, total cost and the full result bytes. `cmd_settle` decodes `ResultPublished` and compares the bytes with the delivered schedule.

Resolution: no action.

### S3.10 Allowance is exact; a leftover allowance can remain

Severity: low

Evidence: `cmd_offer` approves exactly `state.amount` to the escrow. The escrow only pulls `amount` inside `offer`, called by the buyer, so the classic approve race does not apply. If `approve` is broadcast and `offer` later fails its dry run, the allowance stays until `offer` consumes it.

Resolution: accept for the canary. If the order is abandoned after `approve`, the buyer must revoke the allowance by hand. Record that step in the evidence if it happens.

## 4. The proving build

Files: `methods/build.rs`, `methods/Cargo.toml`, `methods/solver-guest/Cargo.toml`, `rust-toolchain.toml`, `Cargo.lock`, `contracts/vendor/risc0`, `contracts/script/DeploySolver.s.sol`, `contracts/test/RealVerifier.t.sol`.

### S4.1 Image ID drift between native and Docker builds

Severity: medium

Evidence: `methods/build.rs` line 10 switches to the pinned RISC Zero Docker build only when `RISC0_USE_DOCKER` is set and not `0`. A native build gives an image ID that depends on the local toolchain. `cmd_deploy` refuses to deploy without `RISC0_USE_DOCKER=1` unless `--allow-local-image-id` is passed. `cmd_deploy` reads `imageId()` back from the deployed escrow and compares it with the built ID. `cmd_prove` compares the proof's image ID with the deployed one. The CI workflow `.github/workflows/solver-bounty.yml` does not set `RISC0_USE_DOCKER`, so the image ID `0x232381f5...` in `solver-bounty/ci-result.json` is a native build ID and is not the deployment ID. The working-tree version of the script additionally records `image_builder` in `state.json` at deploy time, and `cmd_evidence` refuses to write a mainnet `manifest.json` whose builder is not a `risczero/risc0-guest-builder` image.

Open item: no reproducible image ID is recorded at this commit. Plan item 3 must run the Docker build on an x86 host, record the ID from `warrant-host solver-image-id` in the deployment manifest, and deploy with that ID. Do not pass `--allow-local-image-id` on mainnet.

### S4.2 Dev-mode receipts are refused at every stage

Severity: note

Evidence: `host/Cargo.toml` builds `risc0-zkvm` with `disable-dev-mode`. `host/src/main.rs` line 97 exits when `RISC0_DEV_MODE` is present in the environment, with any value. `solver-prove` line 145 requires a succinct receipt. `wrap` line 323 refuses a fake receipt and line 332 requires Groth16. `export-evm` line 343 requires Groth16. `cmd_check` and `cmd_prove` refuse when the variable is set.

Resolution: no action.

### S4.3 Control root pinning matches the prover version

Severity: note

Evidence: `contracts/vendor/risc0/PROVENANCE.json` pins `risc0-ethereum` tag `v3.0.0`, commit `32aa0b6f`, with file hashes. `ControlID.CONTROL_ROOT` is `a54dc85a...1f56`. `Cargo.lock` pins `risc0-zkvm 3.0.5` and `risc0-circuit-recursion 4.0.5`. In that crate, `ALLOWED_CONTROL_ROOT` is the same value, and `BN254_IDENTITY_CONTROL_ID` `c07a6514...4404` is the byte-reversed `BN254_CONTROL_ID` `04446e66...7ac0` in `ControlID.sol`, as the file comment states. `DeploySolver.s.sol` line 23 passes both constants into the verifier constructor, where they are immutable. The verifier computes an immutable `SELECTOR` from them and rejects a seal with another selector (`RiscZeroGroth16Verifier.sol` line 159). `export-evm` prefixes the seal with the prover's selector (`host/src/main.rs` line 347). `RealVerifierTest` verifies a real r0vm 3.0.5 export (`test/fixtures/risc0-3.0.5.json`) against the vendored verifier; it passed in this review. The CI settlement in `ci-result.json` used the same deploy script with the real verifier.

Resolution: no action. Any later bump of `risc0-zkvm` must be paired with a re-check of these two constants.

### S4.4 Toolchain pins

Severity: note

Evidence: `rust-toolchain.toml` pins the workspace to Rust 1.98.1. The guest crate declares `rust-version = "1.88"` and builds with the `rzup` toolchain, or inside Docker. `methods/solver-guest/Cargo.lock` exists and pins `risc0-zkvm 3.0.5`. The canary sets `RISC0_BUILD_LOCKED=1` in `ENV`, so a stale guest lockfile fails the build instead of resolving new versions. `methods/Cargo.toml` pins `risc0-build =3.0.5`. The guest patches `k256`, `sha2` and `crypto-bigint` to fixed RISC Zero commits.

Resolution: no action.

### S4.5 The deploy script reads its inputs from the environment

Severity: note

Evidence: `DeploySolver.run` reads `WARRANT_TOKEN` and `WARRANT_SOLVER_IMAGE_ID` with `vm.envAddress` and `vm.envBytes32`, so a missing or malformed value aborts. `cmd_deploy` sets both from `state.json` and from the freshly built host. After broadcast it verifies `token()` and `imageId()` on chain and records the keccak of the deployed code for escrow and verifier in `cmd_deploy`. The `--retries` option of `forge script` applies to contract verification only, not to transaction broadcast; the script uses `--slow`.

Resolution: no action. The post-deployment code-hash comparison of plan item 4 must use the hashes recorded in `state.json`. The Solidity compiler is 0.8.28 with `evm_version = cancun` (`contracts/foundry.toml`); Arc support for Cancun opcodes is unverified in this review (2026-10-14) and belongs to the read-only probe.

## 5. The signing boundary

Files: `scripts/solver-canary.py`, `scripts/solver-agent.py`, `scripts/test_solver_canary.py`.

### S5.1 Keys stay in a Foundry keystore or a Ledger

Severity: note

Evidence: `parse_role` accepts `account:NAME`, `ledger:0xADDRESS`, and `unlocked:0xADDRESS` only off mainnet. The keystore address is read from the keystore JSON, never the key. `send_tx` and `cmd_deploy` pass `--account` or `--ledger --from` to `cast send` or `forge script`, which sign. The script has no `--private-key` path and does not read `~/.foundry` beyond the address field. `cmd_init` requires four distinct signers on mainnet. The guard tests `test_amount_cap_and_bad_inputs_are_refused` and `test_unlocked_signers_are_allowed_off_mainnet` cover the parsing.

Resolution: no action. The keystore password prompt runs with inherited stdin (`run(..., interactive=True)`); that the prompt works while stdout is piped is unverified here (2026-10-14) and must be confirmed in the local rehearsal before mainnet.

### S5.2 Dry run by default; broadcast needs three matching flags

Severity: note

Evidence: `step` always runs the `eth_call` simulation first. `confirmed` returns false without `--execute` and raises if `--confirm-chain` or `--confirm-amount` differ from `state.json`. Tests `test_confirmation_must_match_state` and `test_dry_run_never_broadcasts` cover both. `connect` compares the RPC chain ID with the state before every chain step.

Resolution: no action.

### S5.3 No step can broadcast twice

Severity: low

Evidence: `step` refuses a name already present in `state.tx`; then saves the intent with `fsync` before calling the broadcast closure. `cast send` waits for one confirmation unless `--async`, which the script does not pass. `test_intent_is_saved_before_broadcast_and_never_retried` covers the sequence. One invocation of `offer --execute` sends two transactions, `approve` then `offer`, each with its own intent record. If a transaction is mined but reverted, `send_tx` raises; the hash is in the error text but not in `state.json`, and a rerun is refused. A `forge script --broadcast` that stops halfway leaves `deploy` recorded as attempted and needs manual `--resume` after inspection.

Resolution: accept. The operator must copy a reverted transaction hash from the error text into the evidence by hand.

### S5.4 The RPC URL does not reach arguments, state or error output

Severity: low

Evidence: `rpc_url` reads `WARRANT_CANARY_RPC` and requires `https://` or loopback. `run` passes it as `ETH_RPC_URL` only to `cast` and `forge`. The same function replaces the URL in failure output with `<rpc>`. Tests `test_errors_do_not_leak_the_rpc_url` and `test_rpc_url_goes_in_the_child_environment_only` cover both. `ENV` copies the whole parent environment, so `cargo`, `warrant-host`, `git` and `solver-agent.py` also inherit `WARRANT_CANARY_RPC`; none of them read or print it.

Open item: whether the Foundry broadcast file copied into `deployment.json` contains the RPC URL is unverified (2026-10-14). Check `deployment.json` for the URL before publishing the evidence folder.

### S5.5 The seller's quote and the relayer cannot influence recipient or amount

Severity: note

Evidence: the recipient is `state.addresses.seller` from `init`, and the amount is `state.amount`; `cmd_quote` requires the quote to repeat the instance hash, amount, seller and checker version exactly, or stops. `cmd_deliver` passes the same seller and amount to `warrant-solver prepare`, which runs `authorize_solver` and fails on any invalid schedule. `cmd_accept` reads the on-chain task and requires buyer, seller, policy hash, amount and both deadlines to match before the seller signs. The relayer only submits the settlement; the contract fixes the recipient (S3.6). `solver-agent.py` receives only the instance and returns a schedule; it sees no key and no RPC.

Resolution: no action.

### S5.6 Approve amount exactness

Severity: note

Evidence: `cmd_offer` simulates and then sends `approve(escrow, amount)` with the exact bounty in token units. No unlimited allowance exists in the script. `cmd_init` bounds `amount` by `HARD_CAP = 10_000_000`, which equals the 10 USDC in the recorded exception. See S3.10 for the leftover case.

Resolution: no action.

### S5.7 Chain guard differs from the plan wording; gas reserve is not quantified

Severity: low

Evidence: plan item 1 asks to refuse chain IDs other than 5042 unless `--allow-chain`. The script instead fixes the chain ID at `init` (default 5042), checks the RPC against it at every chain step, and relaxes signer rules only off mainnet. The effect is equivalent for mainnet. `cmd_check` flags a role with zero gas but does not compare against a reserve. CHAIN-4 asks for an explicit gas reserve.

Resolution: accept the chain guard. Open item: before `deploy`, record each role's gas balance from `check-*.json` and state the reserve in the manifest by hand, as plan step 4 requires.

## Test evidence

Measured on 2026-10-14 at commit `47b05c4` on this machine (macOS, cargo 1.98.1, rustc 1.98.1, forge 1.8.4, Python 3.10.16). Counts come from the command output, not from older documents.

```
cargo test -p warrant-policy --locked
  src/lib.rs unit tests:  3 passed, 0 failed
  tests/invoice.rs:      39 passed, 0 failed
  tests/policy.rs:       17 passed, 0 failed
  tests/solver.rs:       17 passed, 0 failed
  doc-tests:              0 passed, 0 failed
  exit code 0
```

```
cd contracts && forge test --match-path 'test/SolverBountyEscrow.t.sol'
  Suite result: ok. 12 passed; 0 failed; 0 skipped
  (testFuzzAnyMutatedResultByteFails ran 256 fuzz runs)

cd contracts && forge test --match-path 'test/TaskEscrow.t.sol'
  Suite result: ok. 14 passed; 0 failed; 0 skipped

cd contracts && forge test --match-path 'test/RealVerifier.t.sol'
  Suite result: ok. 1 passed; 0 failed; 0 skipped
```

```
python3 scripts/test_solver_canary.py
  Ran 19 tests, OK  (working-tree version, which adds manifest tests; the committed version at 47b05c4 ran 10 tests, OK)
```

Not run in this review: the real proving path (`solver-prove`, `wrap`, `export-evm`), the Docker guest build, `scripts/solver-demo.py` and any transaction on a public chain. The CI record `solver-bounty/ci-result.json` shows one real Groth16 settlement on private Anvil with a native image ID. No proof has settled on Arc testnet or mainnet as of 2026-10-14.

## Summary

| ID | Severity | State |
| --- | --- | --- |
| S1.1 | note | Resolved |
| S1.2 | note | Resolved |
| S1.3 | note | Resolved |
| S1.4 | low | Resolved |
| S1.5 | note | Resolved |
| S1.6 | note | Resolved |
| S2.1 | note | Resolved |
| S2.2 | low | Resolved |
| S2.3 | note | Resolved |
| S2.4 | low | Resolved |
| S2.5 | note | Resolved |
| S2.6 | note | Resolved |
| S3.1 | note | Resolved |
| S3.2 | note | Resolved |
| S3.3 | note | Resolved |
| S3.4 | note | Resolved |
| S3.5 | note | Resolved |
| S3.6 | note | Resolved |
| S3.7 | note | Resolved |
| S3.8 | low | Open: probe Arc USDC behavior before `offer` |
| S3.9 | note | Resolved |
| S3.10 | low | Resolved |
| S4.1 | medium | Open: record the Docker image ID before `deploy` |
| S4.2 | note | Resolved |
| S4.3 | note | Resolved |
| S4.4 | note | Resolved |
| S4.5 | note | Resolved; Cancun support on Arc unverified |
| S5.1 | note | Resolved; keystore prompt unverified until rehearsal |
| S5.2 | note | Resolved |
| S5.3 | low | Resolved |
| S5.4 | low | Open: check `deployment.json` for the RPC URL |
| S5.5 | note | Resolved |
| S5.6 | note | Resolved |
| S5.7 | low | Open: record the gas reserve by hand |

No high finding. One medium finding (S4.1). Four open items block or precede specific canary steps. None requires a code change. This self-review does not replace the independent review that CHAIN-5 requires.
