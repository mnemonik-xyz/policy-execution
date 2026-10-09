"""SQLite event store. It is a cache of the chain: a new scan from the
deployment block rebuilds it, except for intake records and bookings."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
  chain_id INTEGER, tx_hash TEXT, log_index INTEGER,
  block_number INTEGER, block_hash TEXT, block_time INTEGER,
  event TEXT, order_id TEXT, data TEXT,
  PRIMARY KEY (chain_id, tx_hash, log_index));
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, customer TEXT, policy_hash TEXT, po_id TEXT,
  recipient TEXT, max_total INTEGER, settle_by INTEGER, state TEXT);
CREATE TABLE IF NOT EXISTS invoices (
  document_hash TEXT PRIMARY KEY, obligation_id TEXT, invoice_number TEXT,
  seller_tax_id TEXT, po_id TEXT, currency TEXT, payable_minor INTEGER,
  totals_consistent INTEGER, source TEXT, recorded_at INTEGER);
CREATE TABLE IF NOT EXISTS bookings (
  event_key TEXT, adapter TEXT, ledger_ref TEXT, status TEXT, reason TEXT,
  booked_at INTEGER, PRIMARY KEY (event_key, adapter));
CREATE TABLE IF NOT EXISTS cursor (
  chain_id INTEGER, escrow TEXT, next_block INTEGER, last_hash TEXT,
  PRIMARY KEY (chain_id, escrow));
"""


def event_key(chain_id, tx_hash, log_index):
    return f"{chain_id}:{tx_hash.lower()}:{log_index}"


# 2: intake records carry the invoice currency and the payable in its minor units.
VERSION = 2


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name='invoices'").fetchone()[0]
        if has_tables and version != VERSION:
            raise RuntimeError(f"Event store {path} has schema version {version}, expected {VERSION}; "
                               "move it aside and run intake and sync again")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version = {VERSION}")

    def close(self):
        self.db.close()

    # Cursor -------------------------------------------------------------
    def cursor(self, chain_id, escrow):
        return self.db.execute("SELECT next_block, last_hash FROM cursor WHERE chain_id=? AND escrow=?",
                               (chain_id, escrow)).fetchone()

    def set_cursor(self, chain_id, escrow, next_block, last_hash):
        self.db.execute("INSERT INTO cursor VALUES (?,?,?,?) ON CONFLICT(chain_id, escrow) DO UPDATE "
                        "SET next_block=excluded.next_block, last_hash=excluded.last_hash",
                        (chain_id, escrow, next_block, last_hash))

    # Logs and orders ----------------------------------------------------
    def add_log(self, chain_id, log, block, name, fields):
        tx, index = log["transactionHash"].lower(), int(log["logIndex"], 16)
        cur = self.db.execute("INSERT OR IGNORE INTO logs VALUES (?,?,?,?,?,?,?,?,?)",
                              (chain_id, tx, index, block["number"], block["hash"], block["timestamp"],
                               name, fields["orderId"], json.dumps(fields, sort_keys=True)))
        if cur.rowcount and name == "Offered":
            t = fields["terms"]
            self.db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?,?,?,?,?,?)",
                            (fields["orderId"], fields["customer"], t["policyHash"], t["poId"],
                             t["recipient"], t["maxTotal"], t["settleBy"], "Offered"))
        if cur.rowcount and name in ("Accepted", "Closed"):
            self.db.execute("UPDATE orders SET state=? WHERE order_id=?", (name, fields["orderId"]))
        return cur.rowcount == 1

    def events(self, chain_id, names=None, customer=None):
        """Logs in chain order. With a customer, only logs of that customer's orders."""
        query, args = "SELECT * FROM logs WHERE chain_id=?", [chain_id]
        if customer is not None:
            query += " AND order_id IN (SELECT order_id FROM orders WHERE customer=?)"
            args.append(customer.lower())
        rows = self.db.execute(query + " ORDER BY block_number, log_index", args).fetchall()
        out = []
        for r in rows:
            if names and r["event"] not in names:
                continue
            e = dict(r)
            e["data"] = json.loads(r["data"])
            e["key"] = event_key(r["chain_id"], r["tx_hash"], r["log_index"])
            out.append(e)
        return out

    def order(self, order_id):
        r = self.db.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(r) if r else None

    def orders(self, customer=None):
        if customer is None:
            return [dict(r) for r in self.db.execute("SELECT * FROM orders ORDER BY order_id")]
        return [dict(r) for r in self.db.execute("SELECT * FROM orders WHERE customer=? ORDER BY order_id",
                                                 (customer.lower(),))]

    # Intake ------------------------------------------------------------
    def add_invoice(self, facts, source):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?)",
            (facts["documentHash"], facts["obligationId"], facts["invoiceNumber"], facts["sellerTaxId"],
             facts["poId"], facts["currency"], facts["payableMinor"], int(facts["totalsConsistent"]),
             source, int(time.time())))
        return cur.rowcount == 1

    def invoices(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM invoices ORDER BY recorded_at, document_hash")]

    def invoice_by_document(self, document_hash):
        r = self.db.execute("SELECT * FROM invoices WHERE document_hash=?", (document_hash,)).fetchone()
        return dict(r) if r else None

    def invoices_by_obligation(self, obligation_id, po_id=None):
        q, args = "SELECT * FROM invoices WHERE obligation_id=?", [obligation_id]
        if po_id:
            q, args = q + " AND po_id=?", args + [po_id]
        return [dict(r) for r in self.db.execute(q, args)]

    # Bookings ----------------------------------------------------------
    def booking(self, key, adapter):
        r = self.db.execute("SELECT * FROM bookings WHERE event_key=? AND adapter=?", (key, adapter)).fetchone()
        return dict(r) if r else None

    def set_booking(self, key, adapter, status, ledger_ref=None, reason=None):
        self.db.execute("INSERT INTO bookings VALUES (?,?,?,?,?,?) ON CONFLICT(event_key, adapter) DO UPDATE "
                        "SET ledger_ref=excluded.ledger_ref, status=excluded.status, reason=excluded.reason, "
                        "booked_at=excluded.booked_at",
                        (key, adapter, ledger_ref, status, reason, int(time.time())))

    def delete_from(self, chain_id, block_number):
        """Drops logs at and above a block after a reorganization. Orders created
        by dropped Offered logs go too; bookings never exist for them."""
        dropped = [r[0] for r in self.db.execute(
            "SELECT order_id FROM logs WHERE chain_id=? AND block_number>=? AND event='Offered'",
            (chain_id, block_number))]
        self.db.execute("DELETE FROM logs WHERE chain_id=? AND block_number>=?", (chain_id, block_number))
        self.db.executemany("DELETE FROM orders WHERE order_id=?", [(o,) for o in dropped])
        for (order_id,) in self.db.execute("SELECT order_id FROM orders").fetchall():
            last = self.db.execute(
                "SELECT event FROM logs WHERE chain_id=? AND order_id=? AND event IN ('Offered','Accepted','Closed') "
                "ORDER BY block_number DESC, log_index DESC LIMIT 1", (chain_id, order_id)).fetchone()
            self.db.execute("UPDATE orders SET state=? WHERE order_id=?", (last[0] if last else "Offered", order_id))

    def commit(self):
        self.db.commit()
