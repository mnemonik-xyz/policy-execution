# warrant-ledger: book Warrant payments in the buyer's ledger

Status 2026-10-08: phase 0 and phase 1 of
[the ledger integration](../../../ledger-integration/spec.md) are available now.
The beancount export works. The Odoo adapter is planned
([implementation plan](../../../ledger-integration/implementation.md)).

The connector reads public `InvoiceEscrow` logs, records the invoices that the
buyer's agent submits, matches each payment to its invoice and writes the ledger.
It holds no Warrant key. It cannot move money. A fault in it can cause a wrong or
missing ledger entry, and the exceptions report and `bean-check` show it.

## What the beancount export contains

| Source | Entry |
|---|---|
| Intake of a UBL document | `custom "warrant-invoice"` with document hash, obligation ID, invoice number, PO hash and payable amount. No postings |
| `Offered` | Escrow funding: `Assets:Escrow:Warrant` against `Assets:Buyer:Wallet` |
| `Paid` | Payment: `Expenses:Warrant:<vendor>` against the escrow, with transaction, obligation, document and evidence hashes, the authenticator and the match result |
| `Closed` | Refund of the remainder to the wallet |
| After `Closed` | `balance` assertion on the escrow account, tolerance 0.000001 USDC |

The plugin `warrant_ledger.beancount_plugin` runs inside `bean-check`. It fails the
check for a payment without an intake record, and for an intake record without a
payment after `unpaid_after_days`.

## Use

Build the identifier tool once. It needs no zkVM toolchain.

```sh
cargo build -p warrant-ids --release --locked
```

Write a configuration (see
[implementation.md section 2.2](../../../ledger-integration/implementation.md)).
The `[chain]` section must name the buyer:

```toml
[chain]
rpc_url = "http://127.0.0.1:8545"
chain_id = 5042002
escrow = "0x..."      # the InvoiceEscrow deployment
customer = "0x..."    # the buyer address whose orders this ledger books
```

Many buyers can share one `InvoiceEscrow` deployment. The connector reads all
logs of the deployment, but it books and reports only the orders that this
`customer` offered. A payment for an order that the store holds no `Offered`
log for is not booked. Set `from_block` at or before the first offer.

Then run:

```sh
cd connectors/ledger
python3 -m warrant_ledger --config warrant-ledger.toml intake invoice.xml   # each document the agent submits
python3 -m warrant_ledger --config warrant-ledger.toml sync                 # confirmed logs only
python3 -m warrant_ledger --config warrant-ledger.toml export-beancount
python3 -m warrant_ledger --config warrant-ledger.toml report
PYTHONPATH=. bean-check /path/to/warrant.beancount
```

The runtime needs Python 3.11 or later and the standard library only.
`bean-check` needs `pip install beancount==3.2.3`.

## Test

```sh
pip install -r requirements-test.txt pytest
python3 -m pytest tests                      # unit and cross-language tests
# End to end on a local Anvil chain: signed and buyer-approved settlements, then the ledger.
python3 ../../scripts/invoice-demo.py --signed-only --ledger
```

Without the RISC Zero toolchain, build with `RISC0_SKIP_BUILD=1` and set
`WARRANT_DEMO_IMAGE_ID` to any non-zero 32-byte hex value. The demo accepts this
only with `--signed-only`, because those paths never verify a proof.
