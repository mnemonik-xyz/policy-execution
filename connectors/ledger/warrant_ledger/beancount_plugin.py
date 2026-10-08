"""beancount plugin: checks the Warrant entries inside bean-check (spec 8.2).

Enable with: plugin "warrant_ledger.beancount_plugin" "unpaid_after_days=30"
Optional config key as_of=YYYY-MM-DD fixes "today" for reproducible checks."""
import collections
import datetime as dt

from beancount.core import data

__plugins__ = ("check",)

WarrantError = collections.namedtuple("WarrantError", "source message entry")
REQUIRED = ("txid", "obligation", "document", "evidence", "authenticator")


def _config(config_str):
    out = {}
    for part in (config_str or "").replace(",", " ").split():
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def check(entries, options_map, config_str=None):
    cfg = _config(config_str)
    unpaid_days = int(cfg.get("unpaid_after_days", "30"))
    today = dt.date.fromisoformat(cfg["as_of"]) if "as_of" in cfg else dt.date.today()
    invoices, by_obligation, payments, errors = {}, {}, [], []
    for entry in entries:
        if isinstance(entry, data.Custom) and entry.type == "warrant-invoice":
            doc = entry.values[0].value if entry.values else None
            invoices[doc] = entry
            by_obligation.setdefault(entry.meta.get("obligation"), []).append(entry)
        elif isinstance(entry, data.Transaction) and "authenticator" in entry.meta:
            payments.append(entry)
    paid_docs, paid_obligations = set(), set()
    for p in payments:
        missing = [k for k in REQUIRED if k not in p.meta]
        if missing:
            errors.append(WarrantError(p.meta, f"Warrant payment lacks metadata: {', '.join(missing)}", p))
            continue
        paid_docs.add(p.meta["document"])
        paid_obligations.add(p.meta["obligation"])
        if p.meta["document"] not in invoices and not by_obligation.get(p.meta["obligation"]):
            errors.append(WarrantError(p.meta, "Warrant payment without a bill: no intake record for "
                                       f"document {p.meta['document']}", p))
    for doc, inv in invoices.items():
        if doc in paid_docs or inv.meta.get("obligation") in paid_obligations:
            continue
        if (today - inv.date).days > unpaid_days:
            errors.append(WarrantError(inv.meta, f"Warrant bill without a payment for {unpaid_days}+ days: "
                                       f"invoice {inv.meta.get('invoice')}", inv))
    return entries, errors
