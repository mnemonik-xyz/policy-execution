---
name: ionet-warrant-transcription
description: Verified call formats for Warrant audio transcription and buyer guardrail MCP servers.
---

# Warrant io.net Audio Transcription MCP Skill

Guidelines and verified call formats for driving the dual-MCP Warrant audio transcription
workflow across both servers:
1. **Seller Service (`showcases/ionet_arc/seller_mcp_server.py`)**: Hardware discovery, pricing
   proposals, container provisioning, on-chain settlement, and Whisper inference.
2. **Buyer Guardrail (`showcases/ionet_arc/buyer_mcp_server.py`)**: Local policy validation,
   cryptographic policy hash verification, and on-chain escrow funding.

## Core Principles

- **No Destroy Tool**: Deployments use a 1-hour duration billing model (`billing_model: "duration"`)
  and automatically terminate on io.net. Do not search for or call a destroy tool.
- **Repeatable Inference**: While the 1-hour container session is active, `transcribe_audio` can be
  called repeatedly for multiple audio files.
- **Strict 5-Step Lifecycle**:
  1. `list_suitable_hardware` (Seller): Find single-GPU options for Whisper.
  2. `propose_deployment` (Seller): Obtain formal escrow payment terms.
  3. `warrant_evaluate_and_offer` (Buyer): Validate policy, verify hash, and fund escrow.
  4. `deploy_with_escrow` (Seller): Verify deposit, launch container, settle deliverable.
  5. `transcribe_audio` (Seller): Submit audio files for Whisper inference.

---

## Tool Reference

### Buyer Guardrail Tools (`buyer-warrant-mcp-server`)

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
    `policy_hash`, `duration_hours`, `category`, `escrow_address`, `token_address`).
- Returns:
  - `status`: `"APPROVED_AND_OFFERED"` on success (raises `ToolError` on policy violation).
  - `task_id`, `transaction_hash`, `customer`, `amount`, `policy_hash`, `escrow_address`.

#### 3. `warrant_check_escrow_status`
Queries on-chain `TaskEscrow` state to verify task lifecycle.
- Arguments: `task_id` (string, required).
- Returns: `task_id`, `state` (`"Offered"`, `"Accepted"`, `"Paid"`, `"Refunded"`), `amount`,
  `policy_hash`, `recipient`, `customer`.

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
  - `budget_cap_usd` (optional string): Maximum budget ceiling in USD (default: `"1.00"`).
  - `hardware_id` (optional string/int): Specific hardware ID to select.
  - `duration_hours` (optional int): Deployment duration in hours (default: `1`).
- Returns: `task_id`, `salt`, `recipient`, `amount`, `amount_usd`, `duration_hours`,
  `policy_hash`, `escrow_address`, `token_address`.

#### 3. `deploy_with_escrow`
Verifies on-chain escrow funding, provisions the container on io.net, accepts the task on-chain
once provisioned, settles the deliverable with a 384-byte authorization journal, and activates
the transcription worker.
- Arguments: `task_id` (string, required).
- Returns: `status` (`"deployed"`), `deployment_id`, `public_url`, `expires_at`,
  `settlement_transaction`.

#### 4. `transcribe_audio`
Transcribes an audio file using the active 1-hour Whisper deployment on io.net.
- Arguments:
  - `audio_path` (string, required): Local path to audio file (e.g. `./sample.mp3`).
  - `language` (optional string): 2-letter ISO language code (e.g. `"en"`).
- Returns: `status` (`"complete"`), `job_id`, `text`, `language`, `audio_seconds`, `segments`.

#### 5. `get_deployment_status`
Inspects active container health, public URL, and remaining seconds in the 1-hour window.
- Arguments: none (`{}`).
- Returns: `active` (bool), `is_expired` (bool), `remaining_seconds`, `settlement_transaction`.
