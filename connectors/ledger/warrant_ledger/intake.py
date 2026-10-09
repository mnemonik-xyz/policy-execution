"""Intake records: "Warrant saw this invoice". Not a decision."""
import os


def record(store, ids, paths):
    """Records each UBL document once. Returns [(path, facts or None, status)]."""
    out = []
    for path in paths:
        try:
            facts = ids.facts(path)
        except Exception as e:  # DocumentDenied or a tool failure; report, never guess
            out.append((path, None, f"refused: {e}"))
            continue
        new = store.add_invoice(facts, os.path.abspath(path))
        out.append((path, facts, "recorded" if new else "already recorded"))
    store.commit()
    return out
