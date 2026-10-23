# Specification: list the io.net seller in Circle's Agent Marketplace

Status: **proposed, 2026-10-14. Nothing in this document is implemented.**
It applies the [compute marketplace proposal](https://github.com/mnemonik-xyz/warrant/pull/13)
to the seller in [showcases/ionet_arc](https://github.com/mnemonik-xyz/warrant-showcases/blob/main/showcases/ionet_arc/README.md).

Abbreviations: MCP — Model Context Protocol; API — Application Programming
Interface; TLS — Transport Layer Security; EIP — Ethereum Improvement Proposal;
CAIP — Chain Agnostic Improvement Proposal; JSON — JavaScript Object Notation.

## 1. Goal and short answer

Goal: make the io.net seller discoverable through Circle's Discovery API, so that any
buyer agent can find it, obtain a quote, and settle the lease through `TaskEscrow`.

Circle lists x402 endpoints only. The seller today exposes MCP tools over Streamable
HTTP. It has no x402 endpoint and no OpenAPI document. The only x402 code in this
repository is `funding.py`, which parses an io.net payment envelope on the buyer side
and never pays. The seller side of x402 is therefore not developed.

Three deliverables close the gap:

1. A paid HTTP quote endpoint that implements x402 version 2 with Circle's Facilitator Service.
2. An OpenAPI 3.1 document for that endpoint.
3. A listing submission with a public endpoint URL and a payout wallet.

## 2. What exists and what is missing

| Item | State on 2026-10-14 | Source |
|---|---|---|
| Seller MCP tools: `list_suitable_hardware`, `propose_deployment`, `deploy_with_escrow`, `get_deployment_status`, `submit_acceptance`, `transcribe_audio` | Implemented | `seller_mcp_server.py` |
| Quote content | Implemented as the `propose_deployment` result | `seller_mcp_server.py`, `proposal` dict |
| Seller signature on the quote | Missing | — |
| HTTP 402 response with `PAYMENT-REQUIRED` | Missing | — |
| `PAYMENT-SIGNATURE` handling and facilitator settlement | Missing | — |
| Facilitator-Seller-Proof signing | Missing | — |
| OpenAPI document | Missing | — |
| Public HTTPS host | Missing. The README binds `http://<seller-host>:8000`. | `README.md` |
| Payout wallet | Exists: `server_account`, the escrow recipient | `config/manifest.yaml` |
| x402 envelope parsing | Buyer side only, for io.net payments on Solana | `funding.py` |

## 3. Circle requirements, verified 2026-10-13 and 2026-10-14

From Circle's
[get-listed page](https://developers.circle.com/agent-stack/agent-marketplace/get-listed.md):

- The endpoint must return `402 Payment Required` when unpaid and serve the resource when paid.
- The seller publishes an OpenAPI specification, so agents can read inputs and outputs.
- The submission form takes the endpoint URL, the payout wallet address and a description.
- Circle reviews listings manually. Circle sanctions-screens the payout wallet and health-checks the endpoint continuously.

From the live catalogue at `https://api.circle.com/v2/x402/discovery/resources`:

- Every listed resource accepts the `exact` scheme. Arc mainnet `eip155:5042` appears on 2,195 of 2,854 resources. Arc testnet `eip155:5042002` appears on none.
- Resource metadata carries `provider`, `path`, `method`, `description`, `mimeType`, `input` and `output` schemas, and the flags `siwx`, `supportsVanillax402` and `supportsCircleGateway`.

From Circle's
[Facilitator Service quickstart](https://developers.circle.com/facilitator-service/quickstart):

| Fact | Value |
|---|---|
| Settle | `POST https://api.circle.com/v1/facilitator/x402/settle` |
| Status | `GET https://api.circle.com/v1/facilitator/x402/status/{paymentId}` |
| Seller authentication | Header `Facilitator-Seller-Proof`, a base64url envelope with an EIP-712 signature by the `payTo` key |
| EIP-712 domain | name `Circle Facilitator Seller Request`, version `1`, `chainId` of the network |
| Message fields | `purpose`, `method`, `bodyHash`, `network`, `payTo`, `nonce`, `issuedAt`, `expiresAt` |
| Arc testnet | Keyless trial, no Circle account, `eip155:5042002`, USDC `0x3600000000000000000000000000000000000000` |
| Production | `Authorization: Bearer $CIRCLE_API_KEY` |
| Payment identifier | Required, 16 to 128 characters from `[A-Za-z0-9_-]`, reused on retry |
| Settle response | Always HTTP 200. The body says `success`, or `settlement_pending` with a status URL. |
| Rule | Do not fulfil on pending. Poll status until `completed` or `failed`. |

## 4. The quote endpoint

### 4.1 Route and placement

- `POST /x402/quote` on the existing Starlette application, mounted beside the MCP transport.
- `GET /openapi.json` serves the OpenAPI document. It is free.
- Both routes, like the MCP route, have no authentication.
- The endpoint provisions nothing. It only prices and signs.

### 4.2 Request

```json
{
  "customer_address": "0x…",
  "hardware_id": "optional",
  "location_id": "US",
  "duration_hours": 1
}
```

`customer_address` is required, because the task identifier derives from it.
The endpoint rejects unknown fields.

### 4.3 Unpaid request

The server returns status 402 and the header `PAYMENT-REQUIRED`. The header value is
base64 of this object:

```json
{
  "x402Version": 2,
  "resource": {
    "url": "https://<host>/x402/quote",
    "description": "Signed quote for a GPU lease settled through Warrant TaskEscrow on Arc",
    "mimeType": "application/json"
  },
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:5042002",
      "amount": "10000",
      "asset": "0x3600000000000000000000000000000000000000",
      "payTo": "<server_account>",
      "maxTimeoutSeconds": 60,
      "extra": { "name": "USDC", "version": "2" }
    }
  ],
  "extensions": {
    "payment-identifier": {
      "info": { "required": true },
      "schema": {
        "type": "object",
        "properties": {
          "required": { "type": "boolean" },
          "id": { "type": "string", "pattern": "^[A-Za-z0-9_-]{16,128}$" }
        },
        "required": ["id"]
      }
    }
  }
}
```

An extension envelope carries `info` and `schema`, per the x402 version 2 specification.
The client sets `info.id` in its `PaymentPayload`. The server validates it against the
pattern and uses it as the idempotency key. A retry reuses the same identifier.

The quote fee is 0.01 USDC. It is a nominal fee. It exists because Circle lists paid
endpoints only. The network is `eip155:5042002` for the trial and `eip155:5042` for the
listing. One deployment serves one network.

### 4.4 Paid request

1. The client repeats the request with the header `PAYMENT-SIGNATURE`.
2. The server decodes the `PaymentPayload`. It checks `accepted` against its own requirements field by field. It reads the payment identifier from `extensions.payment-identifier.info.id`. A mismatch or a missing identifier returns 402 again.
3. The server builds the Facilitator-Seller-Proof with the `server_account` key and calls settle with that payment identifier.
4. On `success`, the server returns 200, the quote, and the header `PAYMENT-RESPONSE` with the settlement response.
5. On `settlement_pending`, the server polls the status URL up to `maxTimeoutSeconds`. On `completed`, step 4 applies. Otherwise the server returns 402 with an `error`.
6. The server stores the payment identifier with the quote. A repeat of the same identifier returns the same quote and makes no second settle call.

### 4.5 The quote body

The body is the existing `propose_deployment` result, with four additions:

- `quote_id`: the payment identifier.
- `valid_until`: `proposed_at` plus 900 seconds.
- `fulfillment`: the MCP URL, see section 4.6.
- `signature`: a 65-byte secp256k1 signature by the `server_account` key over the SHA-256 of the canonical JSON of all other fields except `fulfillment`. `canonical` and `digest` in `adapter.py` define the encoding.


One invariant holds across the whole path:

```text
payTo in PAYMENT-REQUIRED == recipient in the quote == recovered signer of the quote == recipient of the escrow task
```

The buyer guardrail checks the first three. The escrow enforces the fourth.

### 4.6 The fulfilment path

A discovery client that paid for a quote must be able to complete the deal. The deal
completes through the existing MCP tools `deploy_with_escrow`, `get_deployment_status`,
`submit_acceptance` and `transcribe_audio`. The MCP route has no authentication, so the
paid quote carries no credential, only the endpoint:

```json
{
  "fulfillment": {
    "mcp_url": "https://<host>/mcp",
    "expires_at": 1791900000
  }
}
```

- The `/x402/quote` and `/openapi.json` routes and the MCP route are all open.
- The tools stay safe without a credential on the chain side. `deploy_with_escrow` reads the funded task from the chain. `submit_acceptance` requires the buyer acceptance signature.
- The OpenAPI document describes the `fulfillment` object and names the MCP tools and their argument schemas, so an agent can read the whole flow from the listing.

> [!CAUTION]
> The open MCP route has the risks listed in the
> [mainnet seller section](https://github.com/mnemonik-xyz/warrant-showcases/blob/main/showcases/ionet_arc/README.md#arc-mainnet-seller-canary).
> A paid quote also proves nothing about who uses the endpoint.

### 4.7 Implementation choice

Write the x402 server side in this repository. It is about 150 lines:
header encoding, requirement matching, the EIP-712 seller proof with `eth-account`,
one settle call, one status poll. The `x402` package on PyPI, version 2.25.0 from the
x402 Foundation, offers FastAPI and Flask middleware. This application is Starlette
with the MCP transport. A dependency with its own facilitator client adds more surface
than it removes. Revisit if Circle changes the proof format.

## 5. The OpenAPI document

- OpenAPI 3.1, served at `/openapi.json` and committed at `showcases/ionet_arc/openapi/quote.json` in the `warrant-showcases` repository.
- One path, `POST /x402/quote`, with the request schema of section 4.2.
- Response 402 documents the `PAYMENT-REQUIRED` header. Response 200 documents the quote schema and the `PAYMENT-RESPONSE` header.
- The description states that the quote fee buys a quote, and that the lease settles through `TaskEscrow`.
- The quote schema includes the `fulfillment` object. A section lists the MCP tools of the fulfilment path with their argument schemas.
- A test validates the committed document against the served one.

## 6. The submission

Preconditions:

- A public HTTPS host with a valid TLS certificate. Circle health-checks it continuously. A listing drops while the host is unreachable.
- A payout wallet that passes sanctions screening. This is `server_account`.
- The keyless trial on Arc testnet passes end to end, with the settle transaction recorded in `validation.md`.
- A mainnet deployment of `TaskEscrow` and the verifier, per the [mainnet canary plan](mainnet-canary-plan.md). The catalogue has no testnet entries, so a listing implies `eip155:5042` and a mainnet payout address.

Form content:

- Endpoint URL: `https://<host>/x402/quote`.
- Payout wallet: `server_account` on Arc mainnet.
- Description: a GPU lease on io.net, quoted here and settled through a Warrant escrow on Arc.
- OpenAPI URL: `https://<host>/openapi.json`.
- Category: `INFRASTRUCTURE`.

Circle reviews manually. Circle has not confirmed that it accepts an endpoint whose
product settles outside x402. Ask before the mainnet step. The testnet endpoint is
needed for the guardrail flow in any case.

## 7. Buyer guardrail changes

The guardrail is specified in the compute marketplace proposal. This section lists
only what the quote endpoint requires of it:

- Query Circle Discovery with `category=INFRASTRUCTURE`, `network=eip155:5042` and the provider name.
- Sign an EIP-3009 `transferWithAuthorization` for the quote fee with the buyer wallet. This is a new spend path. It must count against the policy budget.
- Verify the quote signature, and check that the recovered signer equals the quote recipient and the listed `payTo`.
- Reject a quote after `valid_until`.

## 8. Security notes

- The quote route is public and unauthenticated. Rate-limit it per source address.
- The route must not provision, accept or settle. It prices and signs only.
- Never log the `PAYMENT-SIGNATURE` header or the seller proof.
- The seller proof key is the payout key. The seller server already holds it, because it calls `accept` and `settle`. No new key appears.
- A quote fee paid to a copied `payTo` is lost to the buyer. The guardrail must take `payTo` from Circle's listing, not from the 402 header alone.

## 9. Acceptance criteria

- AC-1: An unpaid request returns 402 with a decodable `PAYMENT-REQUIRED` header that matches section 4.3.
- AC-2: A paid request with a mocked facilitator returns 200, a quote, and a `PAYMENT-RESPONSE` header. The quote signature recovers to `server_account`.
- AC-3: A `PaymentPayload` whose `accepted` differs from the requirements returns 402.
- AC-4: A pending settlement returns no quote.
- AC-5: A repeated payment identifier returns the identical quote.
- AC-6: The committed OpenAPI document equals the served one and validates as OpenAPI 3.1.
- AC-7: The invariant of section 4.5 has a test.
- AC-8: One keyless trial settlement on Arc testnet is recorded with its transaction hash.
- AC-9: A 402 challenge without `info.id` in the payload, or with an identifier outside the pattern, is rejected.

## 10. Effort

| Work | Estimate |
|---|---|
| Quote route, 402 and paid path | 2 days |
| Fulfilment token in the quote and on the MCP route | 0.5 day |
| Facilitator client and seller proof | 1 day |
| OpenAPI document and test | 0.5 day |
| Tests for AC-1 to AC-7 | 1 day |
| Guardrail x402 client and checks | 1 day |
| Public HTTPS host | 0.5 day |
| Submission and Circle review | Unknown |

About seven engineer-days plus the review wait. No contract, guest or proof changes.

## 11. Open questions

- Does Circle accept a quote endpoint whose product settles in an escrow?
- Which quote fee is acceptable to buyers and to Circle's review?
- Does the listing need `supportsCircleGateway`? The trial uses vanilla x402.
