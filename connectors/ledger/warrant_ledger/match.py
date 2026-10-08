"""Matches a Paid event to an intake record: document hash first, obligation
identifier second (spec section 7.2)."""

MATCHED = "matched"
BY_OBLIGATION = "matched without the document"
NO_ORDER = "payment without an order"
NO_BILL = "payment without a bill"
AMBIGUOUS = "ambiguous"
CONFLICT = "document and obligation disagree"


def match(store, paid):
    d = paid["data"]
    order = store.order(d["orderId"])
    if order is None:
        return NO_ORDER, None, None
    inv = store.invoice_by_document(d["documentHash"])
    if inv is not None:
        if inv["obligation_id"] != d["obligationId"]:
            return CONFLICT, order, inv
        return MATCHED, order, inv
    candidates = store.invoices_by_obligation(d["obligationId"], order["po_id"])
    if len(candidates) == 1:
        return BY_OBLIGATION, order, candidates[0]
    if len(candidates) > 1:
        return AMBIGUOUS, order, None
    return NO_BILL, order, None
