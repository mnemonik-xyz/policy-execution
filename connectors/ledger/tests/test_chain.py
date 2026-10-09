import shutil
import subprocess

import pytest
from helpers import CUSTOMER, ORDER, RECIPIENT, closed, offered, paid

from warrant_ledger.chain import SIGNATURES, TOPICS, decode


def test_topics_match_signatures():
    if not shutil.which("cast"):
        pytest.skip("Foundry cast not installed")
    for topic, name in TOPICS.items():
        assert subprocess.check_output(["cast", "keccak", SIGNATURES[name]], text=True).strip() == topic


def test_decode_offered_paid_closed():
    po = "0x" + "77" * 32
    name, d = decode(offered(po, 5_000_000_000))
    assert name == "Offered" and d["orderId"] == ORDER and d["customer"] == CUSTOMER
    assert d["terms"]["poId"] == po and d["terms"]["recipient"] == RECIPIENT
    assert d["terms"]["maxTotal"] == 5_000_000_000
    name, d = decode(paid("0x" + "88" * 32, 1_320_000_000, "0x" + "99" * 32, authenticator=2))
    assert name == "Paid" and d["amount"] == 1_320_000_000 and d["authenticator"] == "BuyerApproval"
    assert d["obligationId"] == "0x" + "88" * 32 and d["documentHash"] == "0x" + "99" * 32
    assert decode(closed(7))[1]["refunded"] == 7


def test_unknown_event_is_ignored_and_malformed_is_an_error():
    log = closed(1)
    assert decode({**log, "topics": ["0x" + "00" * 32, ORDER]}) is None
    with pytest.raises(ValueError):
        decode({**log, "data": "0x" + "00" * 64})
    bad = offered("0x" + "77" * 32, 1)
    bad["data"] = bad["data"][:2 + 64 * 3] + "ff" + bad["data"][2 + 64 * 3 + 2:]  # dirty address padding
    with pytest.raises(ValueError):
        decode(bad)
