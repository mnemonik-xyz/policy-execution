"""Calls to the warrant-ids binary. The connector never reimplements the
commitment encoding, so it cannot drift from the checker."""
import json
import subprocess


class DocumentDenied(Exception):
    """The checker would deny this document structurally."""


class Ids:
    def __init__(self, binary="warrant-ids"):
        self.binary = binary

    def _run(self, *args):
        p = subprocess.run([self.binary, *map(str, args)], text=True, capture_output=True)
        if p.returncode == 2:
            raise DocumentDenied(p.stderr.strip())
        if p.returncode != 0:
            raise RuntimeError(f"warrant-ids {args[0]} failed: {p.stderr.strip()}")
        return p.stdout.strip()

    def facts(self, path):
        return json.loads(self._run("facts", path))

    def obligation(self, seller_tax_id, invoice_number):
        return self._run("obligation", seller_tax_id, invoice_number)

    def reference(self, text):
        return self._run("reference", text)

    def tax_id(self, text):
        return self._run("tax-id", text)

    def order_id(self, chain_id, escrow, customer, policy_hash, po_id):
        return self._run("order-id", chain_id, escrow, customer, policy_hash, po_id)
