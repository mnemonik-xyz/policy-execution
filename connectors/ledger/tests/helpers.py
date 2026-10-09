"""Builders for synthetic InvoiceEscrow logs. Test data only."""
from warrant_ledger.chain import TOPICS

TOPIC = {name: topic for topic, name in TOPICS.items()}


def word(value):
    if isinstance(value, int):
        return value.to_bytes(32, "big").hex()
    raw = value[2:]
    return raw.rjust(64, "0")


def log(name, topics, data_words, tx, index, block, block_hash=None):
    return {
        "topics": [TOPIC[name], *[("0x" + word(t)) for t in topics]],
        "data": "0x" + "".join(word(w) for w in data_words),
        "transactionHash": tx, "logIndex": hex(index),
        "blockNumber": hex(block), "blockHash": block_hash or "0x" + f"{block:064x}",
    }


ORDER = "0x" + "aa" * 32
CUSTOMER = "0x" + "22" * 20
RECIPIENT = "0x" + "44" * 20
POLICY = "0x" + "33" * 32


def offered(po_id, max_total, tx="0x01", index=0, block=10, settle_by=2_000_000_000):
    terms = [POLICY, 1, po_id, RECIPIENT, max_total, "0x" + "55" * 20, max_total, max_total, 0, settle_by]
    return log("Offered", [ORDER, CUSTOMER], terms, tx, index, block)


def paid(obligation, amount, document, authenticator=1, tx="0x02", index=0, block=11, evidence="0x" + "66" * 32):
    return log("Paid", [ORDER, obligation], [amount, authenticator, document, evidence], tx, index, block)


def closed(refunded, tx="0x03", index=0, block=12):
    return log("Closed", [ORDER], [refunded], tx, index, block)
