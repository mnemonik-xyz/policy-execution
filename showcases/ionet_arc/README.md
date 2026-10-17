# io.net GPU Workload Pilot with Warrant on Arc

This showcase demonstrates autonomous cloud compute procurement, container deployment, and
on-chain escrow settlement on Arc testnet (chain ID `5042002`) using the Warrant policy framework.
The section [Arc mainnet seller (canary)](#arc-mainnet-seller-canary) describes the hosted
seller for Arc mainnet (chain ID `5042`).

An autonomous buyer LLM agent interacts with an io.net GPU compute seller MCP server. A local
Buyer Warrant MCP guardrail verifies policy rules (budget cap, lease duration, deterministic
policy hash) before funding `TaskEscrow` on Arc. Once escrow is funded, the seller provisions a
`faster-whisper` transcription worker container on io.net, settles escrow upon buyer acceptance
verification, and transcribes audio submitted by the buyer.

> [!IMPORTANT]
> For the manual CLI deployment runbook without MCP, see [README.dev.md](README.dev.md).

---

## Showcase Limitations

This showcase is an infrastructure and agent-interaction pilot. It demonstrates cryptographic
policy evaluation, capability deliverable verification, and dual-MCP orchestration with the
following design trade-offs:

1. **Attested Receipt-Claim Verification (`ReceiptClaimVerifier`)**:
   - On-chain `TaskEscrow` settlement on Arc verifies a 101-byte cryptographic receipt seal
     (`ReceiptClaimVerifier`: `4-byte selector || 32-byte claimDigest || 65-byte ECDSA signature`)
     against the RISC Zero `ReceiptClaim` Merkle structure and pinned guest `IMAGE_ID`, rather than
     full Groth16 proving (which requires Docker and takes 2–4 minutes per proof).
   - Off-chain policy evaluation is **fully real and cryptographic**: the native Rust
     `warrant-host evaluate` binary executes in under 10ms, cryptographically verifying all
     ECDSA authority signatures (`registry.key`, `buyer_acceptance.key`), category limits,
     and budget constraints to produce the canonical 384-byte authorization journal.
2. **No Hardware TEE Attestation**:
   - The compute seller provides **no hardware attestation** (such as AMD SEV-SNP or Intel TDX)
     proving that the container was isolated in a Confidential VM or that io.net allocated
     the claimed GPU model.
   - Capability verification relies on the secret `WARRANT_WORKER_TOKEN` and its matching
     `deliverable_hash = sha256(WARRANT_WORKER_TOKEN)`. Once the buyer agent verifies container
     `/health` with the bearer token, it signs an `Acceptance` statement authorizing settlement.
3. **Escrow Safety with Capability Tokens**:
   - Unlike naive pilots that settle escrow immediately upon deployment, this showcase enforces
     a two-step workflow: the seller accepts the task on-chain to commit terms, provisions the
     container in the background, and can only claim escrow payment after the buyer verifies
     `/health` and signs an `Acceptance` evidence struct off-chain.
   - On-chain settlement requires a valid `ReceiptClaimVerifier` seal signed by the authorized
     prover over the exact chain ID, verifier address, guest `imageId`, and `journalDigest`.
4. **Trust Boundary**:
   - The local Buyer Guardrail strictly prevents unauthorized transactions or spending cap
     violations before funds ever leave the buyer's wallet.
   - For post-settlement transcription, the buyer directly submits inference jobs to the
     authenticated worker during its 1-hour lease window.

---

## The Dual-MCP Architecture: Seller Service & Buyer Guardrail

In an autonomous agent economy, buyers interact with untrusted seller agents. An autonomous
buyer LLM agent cannot be entrusted with unrestricted private keys or unconstrained spending:
hallucinations or prompt injections could drain funds or accept malicious contracts.

To solve this, the architecture separates the remote seller's service from the buyer's local
guardrail sidecar:

```
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │                         Buyer LLM Agent Environment                         │
  │                                                                             │
  │     1. list_suitable_hardware()           3. warrant_evaluate_and_offer()   │
  │     2. propose_deployment()                      (Local Policy Check &      │
  │     4. deploy_with_escrow()                       On-Chain Escrow Funding)  │
  │     5. submit_acceptance()                               │                  │
  └───────────────┬──────────────────────────────────────────┼──────────────────┘
                  │                                          │
                  ▼                                          ▼
   ┌──────────────────────────────┐          ┌──────────────────────────────┐
   │      Seller MCP Server       │          │   Buyer Warrant MCP Server   │
   │  (Remote Streamable HTTP)    │          │    (Local Guardrail Sidecar) │
   │                              │          │                              │
   │ • Discovers io.net GPUs      │          │ • Enforces max budget ($2)   │
   │ • Builds pricing proposals   │          │ • Enforces max duration (1h) │
   │ • Accepts on-chain escrow    │          │ • Verifies policy hash       │
   │ • Provisions Whisper worker  │          │ • Approves & calls offer()   │
   │ • Settles 384-byte journal   │          │ • Tracks escrow state        │
   └──────────────┬───────────────┘          └──────────────┬───────────────┘
                  │                                         │
                  │              Arc Blockchain             │
                  └────────► [ TaskEscrow Contract ] ◄──────┘
```

1. **Seller MCP Server (`seller_mcp_server.py`)**:
   - Hosted remotely by the compute provider over Streamable HTTP (default port 8000).
   - Generates proposals with dynamic hardware pricing, provisions 1-hour Whisper GPU containers,
     and settles deliverables on `TaskEscrow` with a 384-byte authorization journal.
2. **Buyer Warrant MCP Server (`buyer_mcp_server.py`)**:
   - Runs locally alongside the buyer agent as a trusted guardrail sidecar.
   - Holds the buyer's private keys, spending caps, and category whitelist.
   - Independently computes and verifies the cryptographic `warrant-policy` hash before signing
     any transaction.
   - Approves token allowance and submits `TaskEscrow.offer()` on-chain.
   - Supports `stdio` (for Claude Desktop / Cursor / Antigravity) and Streamable HTTP (port 8001).
3. **Arc `TaskEscrow` Contract**: Holds buyer USDC in escrow until valid terms and deliverable
   settlement are executed.
4. **[Transcription Worker](https://github.com/mnemonik-xyz/transcription)**: CUDA/FP16 `faster-whisper` container exposing HTTP
   endpoints (`/health`, `/job`, `/jobs`) with structured operational logs and GPU telemetry.

---

## End-to-End Autonomous Lifecycle

The sequence below illustrates the complete lifecycle between the **Buyer LLM Agent**, the local
**Buyer MCP Guardrail**, the remote **Seller MCP Server**, and the on-chain **`TaskEscrow`**
contract on Arc.

The capability deliverable is the authenticated GPU worker session: the seller generates a secret
`WARRANT_WORKER_TOKEN`, derives `deliverable_hash = sha256(WARRANT_WORKER_TOKEN)`, and provides it
alongside `public_url`. Once the buyer agent verifies container `/health`, the buyer signs an
`Acceptance` statement off-chain, enabling the seller to settle on Arc:

```mermaid
sequenceDiagram
    autonumber
    actor Buyer as Buyer LLM Agent
    participant BuyerMCP as Buyer MCP Server (Guardrail)
    participant SellerMCP as Seller MCP Server (io.net)
    participant Escrow as TaskEscrow (Arc)
    participant Worker as Whisper Worker (io.net)
    participant Host as warrant-host (Evaluator)

    %% Phase 1: Hardware Discovery
    rect rgb(240, 248, 255)
    Note over Buyer,SellerMCP: Phase 1: Hardware Discovery & Selection
    Buyer->>SellerMCP: list_suitable_hardware()
    SellerMCP-->>Buyer: GPU candidates (RTX 4090, hourly rate, replicas)
    Buyer->>Buyer: Selects suitable GPU for transcription (e.g. $0.35/hr)
    end

    %% Phase 2: Quotation & Terms
    rect rgb(255, 250, 240)
    Note over Buyer,SellerMCP: Phase 2: Quotation & Terms
    Buyer->>SellerMCP: propose_deployment(hardware_id, duration_hours=1)
    SellerMCP-->>Buyer: Proposal {task_id, amount, duration_hours, salt, recipient}
    end

    %% Phase 3: Policy Evaluation & Escrow Funding
    rect rgb(240, 255, 240)
    Note over Buyer,Escrow: Phase 3: Policy Evaluation & Escrow Funding
    Buyer->>BuyerMCP: warrant_evaluate_and_offer(proposal)
    BuyerMCP->>BuyerMCP: Checks budget ceiling and 1h duration limits
    BuyerMCP->>BuyerMCP: Instantiates policy template (15m validity window, keys)
    BuyerMCP->>BuyerMCP: Derives cryptographic policy_hash
    BuyerMCP->>Escrow: approve(amount) + offer(task_id, policy_hash, accept_by=5m, settle_by=15m)
    Escrow-->>BuyerMCP: Funds locked in State.Offered (tx hash)
    BuyerMCP-->>Buyer: Offer confirmed {status: "APPROVED", policy, policy_hash}
    end

    %% Phase 4: Order Notification & Seller Acceptance
    rect rgb(255, 245, 245)
    Note over Buyer,Escrow: Phase 4: Order Notification & Seller Acceptance
    Buyer->>SellerMCP: deploy_with_escrow(task_id, policy)
    SellerMCP->>Escrow: Read tasks(task_id) -> verify policy_hash, amount, recipient
    SellerMCP->>Escrow: accept(task_id) on-chain
    Note over Escrow: State.Accepted: agreement committed, buyer cannot cancel
    SellerMCP->>SellerMCP: Generates random WARRANT_WORKER_TOKEN
    SellerMCP->>SellerMCP: Derives deliverable_hash = sha256(WARRANT_WORKER_TOKEN)
    SellerMCP->>Worker: Starts background provisioning on io.net
    SellerMCP-->>Buyer: {status: "provisioning", task_id, deliverable_hash}
    end

    %% Phase 5: Async Polling for Container Launch
    rect rgb(250, 240, 255)
    Note over Buyer,Worker: Phase 5: Async Polling for Container Launch
    loop Poll until status is ready
        Buyer->>SellerMCP: get_deployment_status(task_id)
        SellerMCP-->>Buyer: {status: "provisioning", elapsed_seconds}
    end
    Worker-->>SellerMCP: Container online at public_url
    Buyer->>SellerMCP: get_deployment_status(task_id)
    SellerMCP-->>Buyer: {status: "ready", public_url, worker_token, deliverable_hash}
    end

    %% Phase 6: Health Verification & Deliverable Sign-off
    rect rgb(240, 255, 255)
    Note over Buyer,BuyerMCP: Phase 6: Health Verification & Deliverable Sign-off
    Buyer->>Worker: GET /health (Authorization: Bearer <worker_token>)
    Worker-->>Buyer: 200 OK {"status": "ok"}
    Buyer->>BuyerMCP: warrant_sign_acceptance(task_id, deliverable_hash, recipient, amount)
    BuyerMCP->>BuyerMCP: Signs Acceptance using buyer_acceptance.key
    BuyerMCP-->>Buyer: Signed Acceptance evidence & ECDSA signature
    end

    %% Phase 7: Off-Chain Evaluation & Settlement
    rect rgb(255, 255, 240)
    Note over SellerMCP,Escrow: Phase 7: Off-Chain Evaluation & Settlement
    Buyer->>SellerMCP: submit_acceptance(task_id, acceptance, signature)
    SellerMCP->>Host: warrant-host evaluate(policy, request, evidence) (<10ms)
    Host-->>SellerMCP: 384-byte authorization journal
    SellerMCP->>Escrow: settle(seal, journal) (101-byte ReceiptClaimVerifier seal)
    Escrow->>Escrow: Verifies ReceiptClaim seal & journal, marks State.Paid
    Escrow-->>SellerMCP: Releases USDC to seller recipient
    SellerMCP-->>Buyer: {status: "settled", settlement_transaction}
    end

    %% Phase 8: Workload Execution
    rect rgb(245, 245, 245)
    Note over Buyer,Worker: Phase 8: Workload Execution (1-Hour Direct Inference)
    Buyer->>Worker: POST /job (raw audio + X-Audio-SHA256) with Bearer <worker_token>
    Worker-->>Buyer: 202 Accepted {job_id}
    Buyer->>Worker: GET /job/<job_id> (Authorization: Bearer <worker_token>)
    Worker-->>Buyer: Transcription text and timestamped segments
    Note over Buyer,Worker: Buyer streams audio directly to GPU for the rest of the 1-hour lease
    end
```

---

## Exposed MCP Tools

The dual-MCP architecture separates the remote Seller compute tools from the local Buyer
guardrail sidecar tools:

* **Buyer Guardrail (`buyer-warrant-guardrail`)**: Enforces local budget caps, duration limits,
  and category whitelists, independently compiles policy hashes, submits `TaskEscrow.offer()`,
  and signs deliverable acceptance receipts (`buyer_acceptance.key`).
* **Seller Service (`seller-mcp-server`)**: Discovers io.net GPU hardware, calculates duration
  quotes, provisions containers, evaluates authorization off-chain via native Warrant, and
  settles payment on Arc.

Detailed tool specifications, argument schemas, verified return structures, and agent prompt
guidelines are maintained in the agent skill definition:

**[skills/ionet/SKILL.md](skills/ionet/SKILL.md)**

---

## Desktop Agent Configuration (Claude Desktop, Cursor, Antigravity)

Configure both MCP servers in your agent settings (e.g. `claude_desktop_config.json`).
Because the Buyer Guardrail manages local keys and spending limits, it runs locally via
`stdio`. The Seller Service is hosted remotely by the compute provider over Streamable HTTP:

```json
{
  "mcpServers": {
    "buyer-warrant-guardrail": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": [
        "-m", "showcases.ionet_arc.buyer_mcp_server",
        "--transport", "stdio"
      ]
    },
    "seller-transcription-service": {
      "url": "http://<seller-host>:8000/mcp"
    }
  }
}
```

> [!NOTE]
> If your client does not support direct HTTP MCP URLs, bridge the remote endpoint via
> `mcp-remote`: `"command": "npx", "args": ["-y", "mcp-remote", "http://<seller-host>:8000/mcp"]`.
> For the Arc mainnet seller, see
> [Desktop configuration for mainnet](#desktop-configuration-for-mainnet).

The agent prompts can then naturally discover GPU hardware, request a quote, submit it to
the local Warrant tool for policy check and escrow funding, deploy, and transcribe audio.

---

## Arc mainnet seller (canary)

Status 2026-10-17: live.

The seller MCP for Arc mainnet (chain ID `5042`) runs at `https://seller.mnemonik.xyz/mcp`
behind HTTP basic auth. It uses one shared credential, which the operator hands out; requests

without it get 401. It settles through the no-proof escrow `TaskEscrow` and the test-double
`JournalVerifier` (see "Mainnet addresses"). The verifier checks no signature and no proof, so
a buyer trusts the seller. A signature-checking verifier is not used yet.


> [!CAUTION]
> Risks for everyone who holds the shared credential:
>
> - Anyone with the credential can call every tool. `get_deployment_status` returns the
>   worker token of the active deployment, so a credential holder can use the paid GPU container
>   during the lease.
> - Any credential holder can call `propose_deployment` and `list_suitable_hardware`. Each call reads the
>   io.net API and the seller RPC, and stores a proposal file. Nothing limits the rate, so
>   this is a denial-of-service and disk-growth vector.
> - A credential holder who knows a funded `task_id` can trigger `deploy_with_escrow` and occupy the
>   single deal slot, which blocks other buyers.
> - The seller spends its own io.net credits on provisioning. The 1 USD cap per deal is the
>   only spending limit.
> - The credential is shared. Hand it out only to people you trust, and rotate it if it leaks.

The seller path on mainnet (funding, acceptance, GPU provisioning, settlement) has not run
with money. The first buyer is therefore the first test.

Further limits for the buyer:

- The compiled buyer policy is valid for 900 seconds (`validity_seconds: 900`). Sign the
  acceptance within 15 minutes of the policy compilation.
- The buyer guardrail refuses a proposal above `max_budget_usd` of the manifest (1.00).
  The policy rule comes from
  [`config/buyer_policy_template.yaml`](config/buyer_policy_template.yaml) and allows up to
  2.00 USDC. Do not edit the template: the 1.00 limit comes from the manifest.
- On chain 5042, `deploy_with_escrow`, `get_deployment_status` and `submit_acceptance` need
  the `task_id`. It is the `TaskEscrow` task ID that `propose_deployment` returns: `0x`
  and 64 hex digits.

### Mainnet addresses

All contracts below are published on Arc mainnet (chain ID `5042`). Each address links to the
[Arc explorer](https://explorer.arc.io). The sources are in this repository.

**Seller MCP (used by `https://seller.mnemonik.xyz/mcp` now)**

| Role / Contract | Address or key | Source in this repository |
| --- | --- | --- |
| **TaskEscrow** (no-proof escrow) | [`0x5fae10ac58bf90c17f516292883a4f17c0768002`](https://explorer.arc.io/address/0x5fae10ac58bf90c17f516292883a4f17c0768002) | [`contracts/src/TaskEscrow.sol`](../../contracts/src/TaskEscrow.sol) |
| **JournalVerifier** (test double) | [`0xdfe2fdb1d9942af02f99facfaa5b22d0031ad76c`](https://explorer.arc.io/address/0xdfe2fdb1d9942af02f99facfaa5b22d0031ad76c) | [`contracts/test/PolicyExecutionVault.t.sol`](../../contracts/test/PolicyExecutionVault.t.sol) (`JournalVerifier`) |
| **USDC Token** | [`0x3600000000000000000000000000000000000000`](https://explorer.arc.io/address/0x3600000000000000000000000000000000000000) | Arc system token |
| **Seller** (payout wallet) | [`0xb8202bebb6d8a0a822a56c0062632325c3923eeb`](https://explorer.arc.io/address/0xb8202bebb6d8a0a822a56c0062632325c3923eeb) | |
| **Registry public key** | `0x02264b8d8cdb5b5a45736a4e3f679966966f39979b16794f08530c1ac6d77ee034` | |

**Proven solver canary (real Groth16 proof, settled 2026-10-16)**

| Role / Contract | Address | Source in this repository |
| --- | --- | --- |
| **SolverBountyEscrow** | [`0xbe6b87c0d2a1a7c28207cdecd4f6c68ffe4122e0`](https://explorer.arc.io/address/0xbe6b87c0d2a1a7c28207cdecd4f6c68ffe4122e0) | [`contracts/src/SolverBountyEscrow.sol`](../../contracts/src/SolverBountyEscrow.sol) |
| **RiscZeroGroth16Verifier** | [`0xc4fb7340971e8ad6925f16c2a275cd13cb7bec99`](https://explorer.arc.io/address/0xc4fb7340971e8ad6925f16c2a275cd13cb7bec99) | RISC Zero `risc0-ethereum` v3.0.0 |

The deployment was built from commit `fac8b909893cf168157d84e51ce103fdb0351299`. The deploy record is
[`deployment.json`](../../solver-bounty/canary/mainnet/20261016T202706Z/deployment.json) in the
evidence folder.

**Warrant usage on Arc mainnet: transactions**

The 2 USDC deal ran with a real RISC Zero Groth16 proof. The escrow released the funds only after
the verifier accepted the proof.

| Step | Transaction |
| --- | --- |
| Deploy RiscZeroGroth16Verifier | [`0xead1e9d7...ee189a`](https://explorer.arc.io/tx/0xead1e9d705b13fddfeb8c87c13307e61d9d13d4e5020fc0cfaf31fa12fee189a) |
| Deploy SolverBountyEscrow | [`0x34902ab0...121aaa`](https://explorer.arc.io/tx/0x34902ab030ce99fa8427fdc8b908017bb8ec418b033b0f7dde22105013121aaa) |
| Buyer approves 2 USDC | [`0x9c8104d5...db1ac9`](https://explorer.arc.io/tx/0x9c8104d5a49602ac45f9c6292981b82b1d5d88e6ff79df8acf9726f971db1ac9) |
| Buyer funds the escrow (`offer`) | [`0x204eb61a...3d8279`](https://explorer.arc.io/tx/0x204eb61a89776a6ce764a61770d7c48f2d63df40d8e32657287937738b3d8279) |
| Buyer accepts the proven terms (`accept`) | [`0x346fcfc1...8dac9c`](https://explorer.arc.io/tx/0x346fcfc14d1ece27a022090629a7ace6519f131829472cbe220d88b9648dac9c) |
| Settlement with the Groth16 proof, 2 USDC to the seller | [`0xd0fb5b02...975026`](https://explorer.arc.io/tx/0xd0fb5b024c28ca80e38466917702fdb1b856761cd6cd8bccd58a814699975026) |
| Deploy JournalVerifier (test double) | [`0x457e62dc...685ade`](https://explorer.arc.io/tx/0x457e62dc0a375d87a9a7b27582a0e02e6fd60d27c840aa9a8e0ab6a16b685ade) |
| Deploy TaskEscrow (seller MCP) | [`0x6adc9771...b14735`](https://explorer.arc.io/tx/0x6adc9771ca30d1726e0e08fc9fd553ff205ae4d4a72cf0b18e9cf3f54eb14735) |

The full records are in the evidence folder above.

#### Current deployment and the signature-checking verifier

The seller MCP uses the `TaskEscrow` and the `JournalVerifier` test double in the table above
(deployed 2026-10-17). The test double accepts any digest the seller approved and checks no
signature.

A signature-checking verifier set is also deployed on Arc mainnet. The MCP does **not** use
it yet. The MCP will switch to it in a later release, together with a new seller deploy. Until
then, buyers use the addresses in the table above.

| Contract | Address |
| --- | --- |
| **TaskEscrow** (receipt-verified) | Defined in `manifest.yaml` (`escrow_address`) |
| **ReceiptClaimVerifier** | Defined in `manifest.yaml` (`verifier_address`) |

That set is pinned to the guest image ID
`0xafd4ad2d38c10243e72193f92a5d1ab23f3d2135f32282a9c1e43bf734dc105e`. Its prover address is
`0x9F188809b2ba2e29653693C4069c212a95C1B8c4`, which is not the seller payout wallet above.

The deployment PR also publishes `config/mainnet-public_keys.yaml` (repository path
`showcases/ionet_arc/config/mainnet-public_keys.yaml`). It holds the registry public key only.

### Try the seller MCP

Anyone can ask the live seller for a quote. A quote moves no money.

1. Ask the operator for the basic-auth user and password. They are not published here.
2. Make the header value in your own terminal. Do not paste the password into a chat:

   ```sh
   printf 'Basic %s' "$(printf '%s:%s' "<user>" "<password>" | base64 | tr -d '\n')"
   ```

3. Add the seller to Claude Desktop, Cursor or another MCP client as shown in "Desktop
   configuration for mainnet" below. Put the output of step 2 into `SELLER_AUTH`. The
   `--header` argument must read exactly `"Authorization:${SELLER_AUTH}"`. Restart the
   client and open a new chat.
4. Ask the agent: "Use the seller-transcription-service tools to request a quote for a
   transcription task with a maximum of 1.00 USD. My customer address is `<your wallet
   address>`. Do not fund or accept anything."
5. The seller answers with a proposal: a one-hour GPU deployment, the price (about 0.30 USD),
   the `TaskEscrow` address and a `task_id`. Nothing is funded until you do it.

To buy, follow "Buyer setup" below. You need an Arc mainnet wallet with a little USDC
(native and ERC-20 USDC share one balance on Arc). The seller gives you a GPU
container for one hour. You send audio to it. For a video, extract the audio first, for example
`ffmpeg -i video.mp4 -vn audio.mp3`.

The mainnet seller path (funding, acceptance, GPU provisioning, settlement) has not run with
money yet. The limits and risks above apply.

### Buyer setup

Run every command from the repository root.

1. Build the binaries with `cargo build --release -p warrant-host`. The buyer guardrail
   finds `warrant-policy` and `warrant-evidence` in `target/release/` or `target/debug/`.
   Without the build, it fails with "Could not find compiled binary".
2. Copy [`config/manifest.yaml`](config/manifest.yaml) to your own file outside git. Set the
   fields of the manifest table after step 7.
3. Give the guardrail your own funded Arc wallet key. Use one of these inputs:
   - the flag `--key-file <path>`;
   - the manifest field `private_key_files.buyer_key`;
   - the environment variable `WARRANT_BUYER_KEY`.

   Use `--key-file` or `private_key_files.buyer_key` with a file of mode 0600. Give an
   absolute path: a relative path resolves against the repository root. Never put
   the key in the desktop configuration. Do not use `--private-key`, because the process
   list shows its value.
4. Generate your own acceptance key. Never use a key that another person gave you.

   ```bash
   mkdir -p showcases/ionet_arc/.keys
   (umask 077 && openssl rand -hex 32 > showcases/ionet_arc/.keys/buyer_acceptance.key)
   ```

   The file holds 64 hex characters without `0x`, the same form that `init.py` writes.
   The path `showcases/ionet_arc/.keys/buyer_acceptance.key` is fixed in
   `buyer_mcp_server.py` and `escrow.py`. No flag or manifest field changes it.
5. Get the public part of the acceptance key as `0x` hex. `warrant-evidence public-key`
   prints a JSON array of 33 byte values, so convert it:

   ```bash
   target/release/warrant-evidence public-key showcases/ionet_arc/.keys/buyer_acceptance.key \
     | python3 -c 'import json,sys; print("0x"+bytes(json.load(sys.stdin)).hex())'
   ```

6. Copy the published `config/mainnet-public_keys.yaml` to your own file. Add the line
   `buyer_acceptance_key: '0x<hex from step 5>'`. Set `public_files.public_keys` of your
   manifest to the absolute path of this file. A relative path resolves against the
   repository root first. The published file has no `buyer_acceptance_key`. Each buyer
   signs the acceptance with an own key. A shared key lets other people accept in your name.
7. Configure the desktop agent as shown in the next section.

| Manifest field | Buyer value |
| --- | --- |
| `chain_id` | `5042` |
| `rpc_url` | `https://rpc.mainnet.arc.io`, or your own Arc mainnet RPC |
| `escrow_address` | `0x5fae10ac58bf90c17f516292883a4f17c0768002` |
| `verifier_address` | `0xdfe2fdb1d9942af02f99facfaa5b22d0031ad76c` |
| `token_address` | `0x3600000000000000000000000000000000000000` |
| `category` | `42` |
| `max_budget_usd` | `1.00` |
| `duration_hours` | `1` |
| `validity_seconds` | `900` |
| `buyer_address` | Your own funded Arc wallet. Add this field: the example file does not have it. |
| `public_files.public_keys` | Absolute path of your own public keys file (step 6) |
| `private_key_files.buyer_key` | Optional. Absolute path of your wallet key file (step 3) |

The guardrail does not read the other entries of the example file. `seller_address`,
`vendor_validity_days`, `public_files.vendor_credential` and `public_files.vendor_signature`
are seller-side values. `private_key_files.registry_key`, `private_key_files.buyer_acceptance_key`
and `private_key_files.seller_key` are seller-side key paths that `init.py` and the seller
read on the VM. Ignore them or delete them.

### Desktop configuration for mainnet

```json
{
  "mcpServers": {
    "buyer-warrant-guardrail": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": [
        "-m", "showcases.ionet_arc.buyer_mcp_server",
        "--transport", "stdio",
        "--manifest", "/path/to/your/manifest.yaml",
        "--key-file", "/path/to/your/buyer.key"
      ]
    },
    "seller-transcription-service": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://seller.mnemonik.xyz/mcp",
               "--header", "Authorization:${SELLER_AUTH}"],
      "env": { "SELLER_AUTH": "Basic <base64 of user:password>" }
    }
  }
}
```

The seller endpoint needs HTTP basic auth. Ask the operator for the user and password, then
make the value in your own terminal, so the password does not land in a log or a chat:

```sh
printf '%s:%s' "<user>" "<password>" | base64
```

Put the output after `Basic ` in `SELLER_AUTH`. The header goes in an environment variable
because some clients mishandle spaces inside an argument. Do not commit this file.

### Operator notes

- The seller refuses to start when the RPC does not answer `eth_chainId`. It exits with
  "cannot determine the chain". Check the RPC before you start the seller.
- A blocked deal slot stops new deals. A session in `provisioning` after a crash or a
  restart blocks the slot. So does an unreadable file in `<IONET_STATE_DIR>/sessions/`,
  or a `failed` session with `"deployment_may_exist": true` before its `expires_at`.
  The refusal names the task or the file. To clear the slot:
  1. In the io.net console, make sure that no deployment for that task runs or bills.
     Destroy a deployment that does.
  2. In `<IONET_STATE_DIR>/sessions/<task_id>.json`, set `"status": "failed"` and remove
     `deployment_may_exist`, or set `expires_at` to a past Unix time. Edit the file as the
     container user, and keep the JSON valid.
  3. Move an unreadable file out of `sessions/`.

  No restart is necessary. By design, a failed deal whose io.net deploy state file exists
  blocks the next deal for up to one hour.

---

## Arc and Showcase Parameters

### 1. Network Parameters

Official network parameters for Arc:

| Parameter | Arc mainnet | Arc testnet |
| --- | --- | --- |
| Chain ID | 5042 | 5042002 |
| RPC URL | `https://rpc.mainnet.arc.io` | `https://rpc.testnet.arc.io` |
| USDC ERC-20 | `0x3600000000000000000000000000000000000000` | Same address |
| ERC-20 decimals | 6 | 6 |
| Native gas decimals | 18 | 18 |

Native and ERC-20 USDC share a unified balance on Arc. Warrant token amounts use six-decimal
integer units ($1.00 \text{ USDC} = 1\,000\,000$ atomic units), reserving native USDC for gas fees.

### 2. Showcase Contract & Wallet Addresses

The showcase contracts and demo participant addresses are defined in
[`config/manifest.yaml`](config/manifest.yaml):

| Role / Contract | Address | Description |
| --- | --- | --- |
| **TaskEscrow** | Defined in `manifest.yaml` (`escrow_address`) | Escrow contract on Arc |
| **ReceiptClaimVerifier** | Defined in `manifest.yaml` (`verifier_address`) | Receipt verifier |
| **USDC Token** | `0x3600000000000000000000000000000000000000` | Arc USDC token |
| **Seller** | Defined in `manifest.yaml` (`seller_address`) | io.net payout wallet |
| **Buyer** | Defined in `manifest.yaml` (`buyer_address`) | Escrow funder wallet |

- **Buyer Address (`buyer_address` / `buyer_account`)**: The buyer agent's funded wallet. It
  approves USDC allowance and calls `TaskEscrow.offer()` to fund deposits. This address is
  cryptographically bound into each unique task identifier (`taskId = hash(customer, salt)`).
- **Seller Address (`seller_address` / `server_account`)**: The compute seller's payout wallet
  (io.net service provider). Certified by Registry Authority credentials to receive settlements
  for GPU Whisper transcription (category 42), and receives USDC upon escrow settlement.

---

## Deploying Contracts to Arc (Testnet or Mainnet)

### 1. Build Host and Obtain Guest Image ID

Build the Rust `warrant-host` binary to inspect the policy guest image ID:

```sh
RISC0_BUILD_LOCKED=1 cargo build -p warrant-host --release --locked
IMAGE_ID=$(target/release/warrant-host image-id)
echo "Warrant Guest Image ID: $IMAGE_ID"
```

### 2. Deploy and Verify Contracts

Deploy `TaskEscrow` and the verifier from the `contracts/` directory using Foundry, and verify on
Arc via Blockscout:

```sh
cd contracts

# Target network RPC and Blockscout explorer API:
# For Arc Testnet (chain ID 5042002):
export WARRANT_RPC_URL=https://rpc.testnet.arc.io
VERIFIER_URL=https://explorer.testnet.arc.io/api

# For Arc Mainnet (chain ID 5042):
# export WARRANT_RPC_URL=https://rpc.mainnet.arc.io
# VERIFIER_URL=https://explorer.arc.io/api

export WARRANT_TOKEN=0x3600000000000000000000000000000000000000
export WARRANT_IMAGE_ID="$IMAGE_ID"
export WARRANT_SELLER="<seller-or-prover-address>"
export WARRANT_DEPLOYER="<your-deployer-address>"

# Option A: Full RISC Zero Groth16 Verifier (requires Docker proving; linux/amd64 on ARM):
forge script script/Deploy.s.sol:Deploy --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --broadcast --slow \
  --verify --verifier blockscout --verifier-url "$VERIFIER_URL"

# Option B: ReceiptClaimVerifier (cryptographic receipt-claim verifier for showcase / canary):
forge script script/DeployReceipt.s.sol:DeployReceipt --rpc-url "$WARRANT_RPC_URL" \
  --account warrant-deployer --sender "$WARRANT_DEPLOYER" --broadcast --slow \
  --verify --verifier blockscout --verifier-url "$VERIFIER_URL"
```

> [!NOTE]
> Automated verification (`--verify`) on `explorer.arc.io/api` may be challenged or blocked
> by Cloudflare. The contracts are successfully deployed and functional on-chain regardless;
> verification can be omitted or performed manually via the Blockscout web UI.

---

## Running the Showcase

Both `seller_mcp_server.py` and `buyer_mcp_server.py` automatically read their initial parameters
(`rpc_url`, `escrow_address`, `verifier_address`, `token_address`, `seller_address`, and
`buyer_address`) directly from [`config/manifest.yaml`](config/manifest.yaml) by default (or via
`--manifest <path>`). You can still override `--rpc-url` via CLI arguments.

To run the demo on **Arc Mainnet** (chain ID `5042`) instead of testnet, update `manifest.yaml` (or
pass `--manifest <path>`) with `chain_id: 5042`, `rpc_url: 'https://rpc.mainnet.arc.io'`, your
deployed `escrow_address` and `verifier_address`, and funded `buyer_address`/`seller_address`
wallets.

### 1. Run the Seller MCP Server

Start the Seller MCP daemon:

```sh
# Live io.net Cloud mode:
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 0.0.0.0 --port 8000

# Local / remote SSH server mode (deploys container via SSH and Docker):
.venv/bin/python -m showcases.ionet_arc.seller_mcp_server \
  --host 0.0.0.0 --port 8000 \
  --local-server --ssh-host petertower
```

Execution modes:
- **Live io.net Cloud (default)**: Uses io.net CaaS API credentials (`IONET_API_KEY`) to deploy
  GPU instances dynamically.
- **Local / Remote Server (`--local-server`)**: Deploys the Whisper container via SSH to a GPU
  server (`docker run --gpus all -p 8080:8080`). Accepts `--ssh-host <host>` (default: `localhost`)
  and `--worker-url <url>` (default: `http://<ssh-host>:8080`).

On live networks like Arc Testnet, the seller agent signs `TaskEscrow.accept()` and
`TaskEscrow.settle()` on-chain. Configure the seller private key via any of:
- File: save to `showcases/ionet_arc/.keys/seller.key` (or reference in `manifest.yaml`
  under `private_key_files.seller_key`)
- Environment variable: `export WARRANT_SELLER_KEY="0x..."`
- CLI flag: `--private-key 0x...` or `--key-file <path>`

Optional CLI flags: `--manifest <path>`, `--rpc-url <url>`, `--server-account <address>`,
`--private-key <hex>`, `--key-file <path>`.

### 2. Run the Buyer Warrant MCP Server

Start the Buyer Warrant guardrail sidecar:

```sh
# Run over stdio (standard for desktop LLM agents):
.venv/bin/python -m showcases.ionet_arc.buyer_mcp_server \
  --transport stdio

# Run over Streamable HTTP (default localhost:8001):
.venv/bin/python -m showcases.ionet_arc.buyer_mcp_server \
  --transport streamable-http
```

On live networks such as Arc Testnet, the buyer agent signs on-chain transactions (`USDC.approve`
and `TaskEscrow.offer`) locally. Configure the buyer private key via any of:
- File: save the key to `showcases/ionet_arc/.keys/buyer.key` (or reference in `manifest.yaml`
  under `private_key_files.buyer_key`)
- Environment variable: `export WARRANT_BUYER_KEY="0x..."`
- CLI flag: `--private-key 0x...` or `--key-file <path>`

Optional CLI flags: `--manifest <path>`, `--rpc-url <url>`, `--auto-mint`, `--private-key <hex>`,
`--key-file <path>`.

### 3. Run with an LLM Agent or Headless Test Runner

#### A. Interactive LLM Agent (Claude Desktop, Cursor, Antigravity)

In real-world use, you interact with a real LLM agent configured with the two MCP servers
(the remote Seller service and the local Buyer guardrail sidecar).

Prompt the agent with a natural request:
> *"Discover available single-GPU options on io.net, select the cheapest suitable GPU within my $2*
> *budget, fund escrow on Arc, deploy the Whisper container, and transcribe `./sample.mp3`."*

The LLM autonomously drives the end-to-end lifecycle:
1. Discovers suitable single-GPU hardware on io.net (`seller:list_suitable_hardware`).
2. Requests an agreement proposal with dynamic pricing (`seller:propose_deployment`).
3. Evaluates proposal terms and funds escrow on Arc (`buyer:warrant_evaluate_and_offer`).
4. Commits agreement on-chain and starts container provisioning (`seller:deploy_with_escrow`).
5. Polls until container is online (`seller:get_deployment_status`).
6. Verifies container `/health` and signs deliverable receipt (`buyer:warrant_sign_acceptance`).
7. Submits acceptance to settle escrow on-chain (`seller:submit_acceptance`).
8. Submits audio directly to the Whisper container via `curl` (`POST /job` and `GET /job/<job_id>`).

#### B. Headless Automated Test Runner

For automated integration testing, CI/CD, or headless demonstrations without an LLM GUI,
run the scripted test agent:

```sh
.venv/bin/python -m showcases.ionet_arc.buyer_test_agent \
  --mcp-url http://127.0.0.1:8000/mcp \
  --buyer-mcp-url http://127.0.0.1:8001/mcp \
  --rpc-url https://rpc.testnet.arc.io \
  --audio ./sample.mp3
```

---

## Worker HTTP API and Operational Logs

The [transcription worker](https://github.com/mnemonik-xyz/transcription) HTTP service exposes the following endpoints:

| Request | Authentication | Result |
| --- | --- | --- |
| `GET /health` | None | Process health status |
| `POST /job` | `Bearer WORKER_TOKEN` | Upload raw audio, receive job status and `job_id` |
| `GET /job/<job_id>` | Same token | Status, transcript text, segments, and timing for one job |
| `GET /jobs` | Same token | Status of all submitted jobs |

Uploads require `Content-Length` and `X-Audio-SHA256`. An optional language code may be passed
via `?language=<code>` or `X-Language: <code>`. Maximum upload size is 64 MiB. The `job_id` is
the lowercase SHA-256 hash of the input audio bytes.

The worker writes human-readable operational logs with job ID prefixes, elapsed execution time,
and best-effort `nvidia-smi` GPU telemetry (utilization, memory used/total, temperature, power).
Set `WARRANT_LOG_JSON=1` for raw JSON logs.

---

## Installation and Automated Tests

Python 3.12+ is supported. Install dependencies from the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -r showcases/ionet_arc/requirements.txt
```

Run test suite:

```sh
# Run entire test suite
.venv/bin/python -m unittest discover showcases/ionet_arc -v

# Or run individual suites:
# Buyer Warrant guardrail MCP server unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_buyer_mcp_server -v

# Seller transcription MCP server unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_warrant_transcription -v

# TaskEscrow Web3 client unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_escrow -v

# Core io.net CaaS pilot unit tests
.venv/bin/python -m unittest showcases.ionet_arc.test_pilot -v

# End-to-end integration test (spins up Anvil, Seller MCP, Buyer MCP, Agent)
.venv/bin/python -m unittest showcases.ionet_arc.test_demo_e2e -v
```

Python sources are linted and formatted with [Ruff](https://docs.astral.sh/ruff/):

```sh
.venv/bin/pip install -r showcases/ionet_arc/requirements-dev.txt
.venv/bin/ruff check showcases/ionet_arc
.venv/bin/ruff format --check showcases/ionet_arc
```

---

## Manual CLI Deployment Runbook

For running manual container discovery, price estimation, deployment, and teardown via the
standalone CLI (`scripts/ionet-showcase.py`) without MCP agents, refer to the
[Developer Runbook](README.dev.md).

---

## Primary References

- [io.net Agent Cloud documentation](https://io.net/docs/guides/clouds/agent-cloud)
- [CaaS container deployment reference](https://io.net/docs/reference/caas/deploy-a-container)
- [CaaS price estimation reference](https://io.net/docs/reference/caas/price-estimation)
- [Arc connection details and RPC](https://docs.arc.io/arc/references/connect-to-arc)
- [Arc EVM differences](https://docs.arc.io/integrate/evm-differences)
- [Circle USDC addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses)
- [faster-whisper repository](https://github.com/SYSTRAN/faster-whisper)
