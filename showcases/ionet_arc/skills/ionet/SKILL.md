---
name: ionet-warrant-transcription
description: Guide and verified call formats for the Warrant io.net transcription MCP server.
---

# Warrant io.net Audio Transcription MCP Skill

Guidelines and verified call formats for driving the Warrant audio transcription MCP server
(`scripts/ionet-mcp-server.py`).

## Core Principles

- **No Destroy Tool**: Deployments use a 1-hour duration billing model (`billing_model: "duration"`)
  and automatically terminate on io.net. Do not look for or call a destroy tool.
- **Repeatable Inference**: While the 1-hour container session is active, `transcribe_audio` can be
  called repeatedly for multiple audio files.
- **Strict Prerequisites**: Tools must be called in order:
  1. `list_suitable_hardware`
  2. `propose_deployment`
  3. Customer verifies policy & funds `TaskEscrow.offer()` on-chain
  4. `deploy_with_escrow`
  5. `transcribe_audio`

---

## Tool Reference

### 1. `list_suitable_hardware`
Discovers available single-GPU instances on io.net suitable for `faster-whisper-small` inference,
sorted ascending by price per hour. Multi-GPU clusters and nodes with 0 available replicas are
filtered out.

- Arguments: none (`{}`).
- Returns: Array of hardware items with `hardware_id`, `hardware_name`, `price_per_hour_usd`,
  `available_replicas`, and `location`.

### 2. `propose_deployment`
Selects the cheapest suitable GPU and generates a binding deployment proposal.

- Arguments:
  - `customer_address` (optional string): EVM address of payer. Defaults to first RPC account.
- Returns:
  - `task_id`: Deterministic task ID (`taskIdFor(customer, salt)`).
  - `salt`: Random salt for on-chain offer.
  - `hardware_id`, `hardware_name`, `price_per_hour_usd`.
  - `amount`: Token cost in atomic units (e.g. 1,000,000 for 1.00 USDC).
  - `amount_usd`: Human-readable cost (e.g. `"1.00"`).
  - `duration_hours`: Exactly `1`.
  - `policy_hash`: Hash of the `accepted-contractor-v1` Warrant policy.
  - `escrow_address`: `TaskEscrow` contract address.
  - `token_address`: Payment ERC-20 token address.

### 3. On-Chain Funding (`TaskEscrow.offer`)
Customer agent verifies the proposal against local Warrant policy (budget cap, category, duration).
If approved, customer funds escrow on-chain:
- `IERC20(token).approve(escrow, amount)`
- `TaskEscrow.offer(salt, recipient, policyHash, policyVersion, amount, acceptBy, settleBy)`

### 4. `deploy_with_escrow`
Verifies on-chain escrow funding, accepts the task, provisions the container on io.net, settles the
escrow deliverable with a 384-byte authorization journal, and activates the transcription service.

- Arguments: `task_id` (string).
- Returns: `status` (`"deployed"`), `deployment_id`, `public_url`, `expires_at`,
  `settlement_transaction`.

### 5. `transcribe_audio`
Transcribes an audio file using the active 1-hour Whisper deployment.

- Arguments:
  - `audio_path` (string, required): Local path to audio file (e.g. `adv.mp3`).
  - `language` (string, optional): 2-letter ISO code (e.g. `"en"`).
- Returns: `status` (`"complete"`), `job_id`, `text`, `language`, `audio_seconds`, `segments`.

### 6. `get_deployment_status`
Inspects active deployment health and remaining duration in the 1-hour window.

- Arguments: none (`{}`).
- Returns: `active` (bool), `is_expired` (bool), `remaining_seconds`, `settlement_transaction`.
