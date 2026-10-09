"""warrant-ledger: books Warrant InvoiceEscrow events into the buyer's ledger."""
import argparse
import json
import os
import sys

from . import config as config_mod
from . import intake, report
from .chain import Rpc
from .ids import Ids
from .store import Store
from .sync import rescan, sync


def main(argv=None):
    parser = argparse.ArgumentParser(prog="warrant-ledger", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("WARRANT_LEDGER_CONFIG", "warrant-ledger.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="read new confirmed logs")
    r = sub.add_parser("rescan", help="drop logs from a block after a deep reorganization")
    r.add_argument("--from", dest="from_block", type=int, required=True)
    i = sub.add_parser("intake", help="record UBL documents the agent submitted")
    i.add_argument("files", nargs="+")
    sub.add_parser("export-beancount", help="rewrite the beancount file")
    rp = sub.add_parser("report", help="exceptions report")
    rp.add_argument("--json", action="store_true")
    sub.add_parser("check", help="self-checks only; exit 1 on a problem")
    args = parser.parse_args(argv)

    cfg = config_mod.load(args.config)
    store = Store(cfg.store)
    try:
        if args.command == "sync":
            print(f"{sync(Rpc(cfg.chain.rpc_url), store, cfg.chain)} new logs")
        elif args.command == "rescan":
            rescan(store, cfg.chain, args.from_block)
            print(f"Logs from block {args.from_block} dropped; run sync")
        elif args.command == "intake":
            for path, facts, status in intake.record(store, Ids(cfg.warrant_ids), args.files):
                print(f"{status}: {path}" + (f" ({facts['invoiceNumber']}, {facts['documentHash']})" if facts else ""))
        elif args.command == "export-beancount":
            if cfg.beancount is None:
                sys.exit("No [beancount] section in the configuration")
            from .adapters import beancount
            print(beancount.export(store, cfg.chain, cfg.beancount, cfg.vendors))
        elif args.command == "report":
            days = cfg.beancount.unpaid_after_days if cfg.beancount else 30
            items = report.exceptions(store, cfg.chain.chain_id, unpaid_after_days=days, customer=cfg.chain.customer)
            if args.json:
                print(json.dumps(items, indent=2, sort_keys=True))
            else:
                for item in items:
                    print(f"{item['kind']}: " + ", ".join(f"{k}={v}" for k, v in sorted(item.items()) if k != "kind"))
                print(f"{len(items)} exceptions")
        elif args.command == "check":
            problems = report.self_checks(store, cfg.chain.chain_id, cfg.chain.customer)
            for p in problems:
                print(f"{p['orderId']}: {p['detail']}")
            sys.exit(1 if problems else 0)
    finally:
        store.close()


if __name__ == "__main__":
    main()
