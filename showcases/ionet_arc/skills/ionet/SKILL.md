---
name: ionet-warrant-transcription
description: Verified call formats for Warrant audio transcription and buyer guardrail MCP servers.
---

# Warrant io.net Audio Transcription MCP Skill

Guidelines and verified call formats for driving the dual-MCP Warrant audio transcription
workflow across both servers:
1. **Seller Service (`seller-mcp-server`)**: Hardware discovery, pricing proposals, container
   provisioning, and on-chain settlement.
2. **Buyer Guardrail (`buyer-warrant-guardrail`)**: Local policy validation, cryptographic policy
   hash verification, on-chain escrow funding, and deliverable sign-off.

## Core Principles

- **No Destroy Tool**: Deployments use a 1-hour duration billing model (`billing_model: "duration"`)
  and automatically terminate on io.net. Do not search for or call a destroy tool.
- **Capability-Based Two-Step Escrow**: Escrow is **not** settled on deployment. The seller commits
  to the agreement on-chain (`TaskEscrow.accept`), provisions the container in the background, and
  claims escrow payment only after the buyer verifies `/health` and signs an `Acceptance` receipt.
- **Direct Worker Inference**: Once provisioned, the buyer operates the container directly over
  HTTP (`curl`) using `public_url` and `worker_token` for the duration of the 1-hour lease.
- **Strict 8-Step Lifecycle**:
  1. `list_suitable_hardware` (Seller): Find single-GPU options for Whisper.
  2. `propose_deployment` (Seller): Obtain formal escrow payment terms.
  3. `warrant_evaluate_and_offer` (Buyer): Validate policy, verify hash, and fund escrow on-chain.
  4. `deploy_with_escrow` (Seller): Commit agreement on-chain and start background provisioning.
  5. `get_deployment_status` (Seller): Poll until status is `"ready"`, getting URL and token.
  6. Health Check (`curl`): Verify `GET <public_url>/health` on the worker endpoint.
  7. Deliverable Settlement: Call `warrant_sign_acceptance` (Buyer) and `submit_acceptance`
     (Seller) to sign evidence and settle on-chain.
  8. Direct Transcription (`curl`): Upload audio to `POST <public_url>/job` and poll
     `GET <public_url>/job/<job_id>` with `Authorization: Bearer <worker_token>`.

---

## Tool Reference

### Buyer Guardrail Tools (`buyer-warrant-guardrail`)

#### 1. `warrant_get_policy`
Inspect local spending constraints and configured buyer wallet before requesting quotes.
- Arguments: none (`{}`).
- Returns: `buyer_account`, `max_budget_usd`, `max_duration_hours`, `allowed_categories`,
  `chain_id`, `escrow_address`, `token_address`, `auto_mint`.

#### 2. `warrant_evaluate_and_offer`
Validates a seller's proposal against local constraints (budget $\le \$2$, duration $\le 1\text{h}$,
category in whitelist), verifies `task_id` matches `taskIdFor`, independently derives/verifies
the cryptographic `warrant-policy` hash, approves ERC-20 token allowance, and submits
`TaskEscrow.offer()` on-chain.
- Arguments:
  - `proposal` (object, required): Full proposal object returned from `propose_deployment`.
    Alternatively accepts unpacked fields (`task_id`, `amount`, `amount_usd`, `salt`, `recipient`,
    `duration_hours`, `category`, `escrow_address`, `token_address`).
- Returns:
  - `status`: `"APPROVED"` on success (raises `ToolError` on policy violation).
  - `task_id`, `transaction_hash`, `customer`, `recipient`, `amount`, `amount_usd`, `policy`,
    `policy_hash`, `policy_version`, `escrow_address`, `token_address`, `accept_by`, `settle_by`.

#### 3. `warrant_sign_acceptance`
Signs an off-chain `Acceptance` deliverable receipt for an offered task using the buyer's private
acceptance key (`buyer_acceptance.key`). Required by compute seller before on-chain settlement.
- Arguments:
  - `task_id` (string, required): Task identifier.
  - `deliverable_hash` (string, required): Lowercase SHA-256 hash of `WARRANT_WORKER_TOKEN`.
  - `recipient` (string, required): Payout address of seller.
  - `amount` (int/str, required): Atomic token amount matching offered task.
- Returns:
  - `status`: `"signed"`.
  - `task_id`, `acceptance` (object), `signature` (array of bytes).

#### 4. `warrant_check_escrow_status`
Queries on-chain `TaskEscrow` state to verify task lifecycle.
- Arguments: `task_id` (string, required).
- Returns: `task_id`, `state` (`"Offered"`, `"Accepted"`, `"Paid"`, `"Refunded"`), `state_idx`,
  `customer`, `recipient`, `amount`, `policy_hash`, `policy_version`, `accept_by`, `settle_by`,
  `escrow_address`.

---

### Seller Service Tools (`seller-mcp-server`)

#### 1. `list_suitable_hardware`
Discovers available single-GPU instances on io.net suitable for `faster-whisper-small` inference,
sorted ascending by price per hour. Multi-GPU clusters and 0-replica nodes are filtered out.
- Arguments: none (`{}`).
- Returns: Array of hardware items with `hardware_id`, `hardware_name`, `price_per_hour_usd`,
  `available_replicas`, and `location`.

#### 2. `propose_deployment`
Selects the cheapest suitable GPU (or requested hardware) and generates a binding proposal
calculated from actual hardware hourly pricing and duration.
- Arguments:
  - `customer_address` (optional string): EVM address of payer. Defaults to first RPC account.
  - `budget_cap_usd` (optional string): Maximum budget ceiling in USD (default: from manifest).
  - `hardware_id` (optional string/int): Specific hardware ID to select.
  - `duration_hours` (optional int): Deployment duration in hours (default: from manifest).
  - `location_id` (optional string): Region identifier (default: `"US"`).
- Returns: `status` (`"proposed"`), `task_id`, `salt`, `customer`, `recipient`, `amount`,
  `amount_usd`, `duration_hours`, `escrow_address`, `token_address`, `verifier_address`,
  `chain_id`.

#### 3. `deploy_with_escrow`
Verifies on-chain escrow deposit, validates that the buyer policy hashes to the on-chain
`policyHash`, commits agreement via `TaskEscrow.accept()`, generates `WARRANT_WORKER_TOKEN` and
deliverable hash, launches background container provisioning, and returns initial session state.
- Arguments:
  - `task_id` (string, required): Task ID from `propose_deployment`.
  - `policy` (object, required): Concrete buyer policy stamped by buyer guardrail.
- Returns: `status` (`"provisioning"` or `"ready"`), `task_id`, `deployment_id`, `public_url`,
  `deliverable_hash`, `message`.

#### 4. `get_deployment_status`
Inspects active container provisioning status, health, and remaining seconds in the 1-hour window.
- Arguments: `task_id` (optional string).
- Returns:
  - When provisioning: `active` (`false`), `status` (`"provisioning"`), `task_id`, `deployment_id`,
    `deliverable_hash`, `elapsed_seconds`, `message`.
  - When ready: `active` (`true`), `status` (`"ready"`), `task_id`, `deployment_id`, `public_url`,
    `worker_token`, `deliverable_hash`, `hardware_id`, `remaining_seconds`, `is_expired`,
    `settlement_transaction`.
  - When failed: `active` (`false`), `status` (`"failed"`), `error`, `message`.

#### 5. `submit_acceptance`
Submits buyer-signed `Acceptance` evidence. Verifies policy rules and authority signatures
off-chain via native `warrant-host evaluate`, approves the 384-byte authorization journal on
`JournalVerifier`, and settles payment on `TaskEscrow` on-chain.
- Arguments:
  - `task_id` (string, required): Task identifier.
  - `acceptance` (object, required): `Acceptance` struct signed by buyer.
  - `signature` (array of ints or hex string, required): Buyer signature on acceptance.
- Returns: `status` (`"settled"`), `task_id`, `settlement_transaction` (tx hash), `message`.

---

### Worker Container HTTP Operations (`curl`)

Once `get_deployment_status` returns `status: "ready"`, use `public_url` and `worker_token` to
interact directly with the container via `curl`:

#### 1. Health Verification (`GET /health`)
```sh
curl -A "Mozilla/5.0" "<public_url>/health"
```

#### 2. Submit Audio Job (`POST /job`)
Uploads require `Content-Length`, `X-Audio-SHA256` (lowercase 64-hex SHA-256 of the raw audio
bytes), and `Authorization: Bearer <worker_token>`:
```sh
AUDIO_FILE="sample.mp3"
CHECKSUM=$(shasum -a 256 "$AUDIO_FILE" | awk '{print $1}')

curl -X POST "<public_url>/job?language=en" \
  -A "Mozilla/5.0" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Audio-SHA256: $CHECKSUM" \
  --data-binary @"$AUDIO_FILE"
```
Returns `202 Accepted` (or `200 OK` if duplicate) with `job_id` (equal to `CHECKSUM`).

#### 3. Poll Transcription Result (`GET /job/<job_id>`)
```sh
curl -A "Mozilla/5.0" \
  -H "Authorization: Bearer $WORKER_TOKEN" \
  "<public_url>/job/$CHECKSUM"
```
Poll until `status` is `"complete"`, which returns `result.language`, `result.audio_seconds`,
`result.segments`, and `elapsed_seconds`.
