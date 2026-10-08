"""Exceptions report (spec section 10) and self-checks (spec section 9)."""
import time

from . import match as m

DAY = 86400


def self_checks(store, chain_id):
    problems = []
    paid, refunded = {}, {}
    for e in store.events(chain_id, ("Paid", "Closed")):
        o = e["data"]["orderId"]
        if e["event"] == "Paid":
            paid[o] = paid.get(o, 0) + e["data"]["amount"]
        else:
            refunded[o] = refunded.get(o, 0) + e["data"]["refunded"]
    for order in store.orders():
        o, spent = order["order_id"], paid.get(order["order_id"], 0)
        if spent > order["max_total"]:
            problems.append({"kind": "self-check", "orderId": o,
                             "detail": f"paid {spent} exceeds maxTotal {order['max_total']}"})
        if o in refunded and spent + refunded[o] != order["max_total"]:
            problems.append({"kind": "self-check", "orderId": o,
                             "detail": f"paid {spent} + refunded {refunded[o]} != maxTotal {order['max_total']}"})
    return problems


def exceptions(store, chain_id, unpaid_after_days=30, expiring_days=7, now=None):
    now = int(now if now is not None else time.time())
    out, paid_docs, paid_obligations = [], set(), set()
    for e in store.events(chain_id, ("Paid",)):
        d = e["data"]
        status, order, inv = m.match(store, e)
        base = {"event": e["key"], "orderId": d["orderId"], "obligationId": d["obligationId"],
                "documentHash": d["documentHash"], "amount": d["amount"]}
        if inv is not None:
            paid_docs.add(inv["document_hash"])
            paid_obligations.add(inv["obligation_id"])
        if status != m.MATCHED:
            out.append({**base, "kind": status})
        if inv is not None and inv["payable"] is not None and inv["payable"] != d["amount"]:
            out.append({**base, "kind": "amount difference", "invoicePayable": inv["payable"]})
        if d["authenticator"] == "BuyerApproval":
            out.append({**base, "kind": "buyer approval: the checker did not run on chain"})
    open_by_po = {}
    for inv in store.invoices():
        if inv["document_hash"] in paid_docs or inv["obligation_id"] in paid_obligations:
            continue
        open_by_po.setdefault(inv["po_id"], []).append(inv)
        if now - inv["recorded_at"] > unpaid_after_days * DAY:
            out.append({"kind": "bill without a payment", "documentHash": inv["document_hash"],
                        "obligationId": inv["obligation_id"], "invoiceNumber": inv["invoice_number"]})
    for order in store.orders():
        if order["state"] == "Accepted" and open_by_po.get(order["po_id"]) and \
                0 <= order["settle_by"] - now <= expiring_days * DAY:
            out.append({"kind": "order expiring with open bills", "orderId": order["order_id"],
                        "settleBy": order["settle_by"], "openBills": len(open_by_po[order["po_id"]])})
    return out + self_checks(store, chain_id)
