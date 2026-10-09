import datetime as dt

import pytest
from helpers import closed, offered, paid

from warrant_ledger import intake, report
from warrant_ledger.adapters import beancount as bc
from warrant_ledger.config import Beancount, Chain
from warrant_ledger.ids import Ids
from warrant_ledger.store import Store
from warrant_ledger.sync import DeepReorg, sync

T0 = int(dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc).timestamp())


class FakeRpc:
    def __init__(self, logs, head, chain_id=5042002):
        self._logs, self.head, self.chain_id, self.forks = logs, head, chain_id, {}

    def call(self, method, params=()):
        assert method == "eth_chainId"
        return hex(self.chain_id)

    def block_number(self):
        return self.head

    def block(self, n):
        return {"number": n, "hash": self.forks.get(n, "0x" + f"{n:064x}"), "timestamp": T0 + (n - 10) * 86400}

    def logs(self, address, lo, hi):
        return [x for x in self._logs if lo <= int(x["blockNumber"], 16) <= hi]


@pytest.fixture
def world(tmp_path, warrant_ids, invoice_xml):
    facts = Ids(warrant_ids).facts(invoice_xml)
    logs = [offered(facts["poId"], 5_000_000_000, block=10),
            paid(facts["obligationId"], facts["payableMinor"] * 10_000, facts["documentHash"], tx="0x02", block=11),
            paid("0x" + "ab" * 32, 1_000_000, "0x" + "cd" * 32, authenticator=2, tx="0x03", block=11),
            closed(5_000_000_000 - facts["payableMinor"] * 10_000 - 1_000_000, tx="0x04", block=12)]
    chain = Chain(rpc_url="", chain_id=5042002, escrow="0x" + "11" * 20, customer="0x" + "22" * 20, from_block=0, confirmations=2)
    store = Store(str(tmp_path / "events.sqlite"))
    return dict(facts=facts, logs=logs, chain=chain, store=store, ids=Ids(warrant_ids), xml=invoice_xml,
                cfg=Beancount(path=str(tmp_path / "books.beancount")))


def test_sync_waits_for_confirmations_and_is_idempotent(world):
    rpc = FakeRpc(world["logs"], head=12)
    assert sync(rpc, world["store"], world["chain"]) == 1          # only block 10 is confirmed
    rpc.head = 14
    assert sync(rpc, world["store"], world["chain"]) == 3
    assert sync(rpc, world["store"], world["chain"]) == 0
    assert world["store"].order(world["logs"][0]["topics"][1])["state"] == "Closed"


def test_deep_reorg_stops(world):
    rpc = FakeRpc(world["logs"], head=14)
    sync(rpc, world["store"], world["chain"])
    rpc.forks[12] = "0x" + "ee" * 32
    rpc.head = 20
    with pytest.raises(DeepReorg):
        sync(rpc, world["store"], world["chain"])


def test_wrong_chain_is_refused(world):
    with pytest.raises(RuntimeError, match="chain"):
        sync(FakeRpc(world["logs"], head=14, chain_id=1), world["store"], world["chain"])


def test_match_report_and_beancount(world):
    store, chain = world["store"], world["chain"]
    sync(FakeRpc(world["logs"], head=14), store, chain)
    assert intake.record(store, world["ids"], [world["xml"]])[0][2] == "recorded"
    assert intake.record(store, world["ids"], [world["xml"]])[0][2] == "already recorded"

    kinds = sorted(i["kind"] for i in report.exceptions(store, chain.chain_id, now=T0 + 5 * 86400, customer=chain.customer))
    assert kinds == ["buyer approval: the checker did not run on chain", "payment without a bill"]

    text = bc.render(store, chain, world["cfg"], {})
    assert text == bc.render(store, chain, world["cfg"], {})          # stable bytes
    assert "1320.000000 USDC" in text and "balance Assets:Escrow:Warrant  0.000000 ~ 0.000001 USDC" in text

    from beancount import loader
    as_of = (dt.date(2026, 10, 1) + dt.timedelta(days=3)).isoformat()
    text = text.replace('"unpaid_after_days=30"', f'"unpaid_after_days=30 as_of={as_of}"')
    entries, errors, _ = loader.load_string(text)
    messages = [e.message for e in errors]
    assert len(messages) == 1 and "payment without a bill" in messages[0], messages


def test_unpaid_bill_is_reported(world, tmp_path):
    store, chain = world["store"], world["chain"]
    intake.record(store, world["ids"], [world["xml"]])
    later = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 40 * 86400
    kinds = [i["kind"] for i in report.exceptions(store, chain.chain_id, now=later, customer=chain.customer)]
    assert kinds == ["bill without a payment"]


def test_self_check_flags_overspend(world):
    store, chain = world["store"], world["chain"]
    logs = world["logs"][:1] + [paid("0x" + "01" * 32, 6_000_000_000, "0x" + "02" * 32, tx="0x09", block=11)]
    sync(FakeRpc(logs, head=14), store, chain)
    assert any("exceeds maxTotal" in p["detail"] for p in report.self_checks(store, chain.chain_id, chain.customer))


def test_export_writes_atomically(world):
    store, chain = world["store"], world["chain"]
    sync(FakeRpc(world["logs"], head=14), store, chain)
    path = bc.export(store, chain, world["cfg"], {"0x" + "44" * 20: "AcmeLV"})
    text = open(path).read()
    assert "Expenses:Warrant:AcmeLV" in text
    with pytest.raises(ValueError):
        bc.render(store, chain, world["cfg"], {"0x" + "44" * 20: "acme lv"})


def test_usd_amount_difference_is_reported(world):
    store, chain, facts = world["store"], world["chain"], world["facts"]
    logs = [world["logs"][0], paid(facts["obligationId"], 1_000_000, facts["documentHash"], tx="0x05", block=11)]
    sync(FakeRpc(logs, head=14), store, chain)
    intake.record(store, world["ids"], [world["xml"]])
    diffs = [i for i in report.exceptions(store, chain.chain_id, now=T0, customer=chain.customer) if i["kind"] == "amount difference"]
    assert diffs and diffs[0]["invoicePayable"] == 1_320_000_000


def test_old_store_is_refused(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE invoices (document_hash TEXT)")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="schema version"):
        Store(str(path))


def test_other_customers_orders_are_not_booked(world):
    store, chain = world["store"], world["chain"]
    other = "0x" + "99" * 20
    foreign = offered("0x" + "77" * 32, 9_000_000_000, tx="0x21", block=10)
    foreign["topics"] = [foreign["topics"][0], "0x" + "88" * 32, "0x" + "00" * 12 + "99" * 20]
    foreign_paid = paid("0x" + "ab" * 32, 2_000_000, "0x" + "cd" * 32, tx="0x22", block=11)
    foreign_paid["topics"] = [foreign_paid["topics"][0], "0x" + "88" * 32] + foreign_paid["topics"][2:]
    sync(FakeRpc(world["logs"] + [foreign, foreign_paid], head=14), store, chain)
    assert store.order("0x" + "88" * 32)["customer"] == other
    mine = bc.render(store, chain, world["cfg"], {})
    assert "9000.000000" not in mine and "2.000000 USDC" not in mine
    assert "0x" + "88" * 32 not in mine and ("0x" + "88" * 32)[:10] not in mine
    assert all(i.get("orderId") != "0x" + "88" * 32 for i in report.exceptions(store, chain.chain_id, now=T0, customer=chain.customer))
    chain.customer = other
    theirs = bc.render(store, chain, world["cfg"], {})
    assert "9000.000000 USDC" in theirs and "1320.000000" not in theirs


def test_line_breaks_in_strings_are_escaped():
    assert bc.q('a\nb\rc"d') == '"a\\nb\\rc\\"d"'
