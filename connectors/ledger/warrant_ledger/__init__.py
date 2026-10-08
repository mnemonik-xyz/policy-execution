"""Books Warrant InvoiceEscrow events into the buyer's ledger.

The connector reads public chain logs, records the invoices the buyer's agent
submitted, and writes ledger entries. It holds no Warrant key and cannot move
money. See warrant/ledger-integration/spec.md.
"""
__version__ = "0.1.0"
