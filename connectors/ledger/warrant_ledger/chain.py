"""Reads InvoiceEscrow logs over JSON-RPC and decodes them.

Topics were computed with `cast sig-event` and match `forge inspect ... events`;
tests/test_chain.py rechecks them when Foundry is present."""
import json
import urllib.request

TOPICS = {
    "0x1eef5bb024bbd29df709047108addd41787391459d51237fb7d4ca0c120f6599": "Offered",
    "0x1f3c0697c3ada95f9e84a917995664b76cd8b4ae5de25e77ee111122ae3a00d0": "Accepted",
    "0x92e306b2023ee1f9fe6a3bbf4034d89cd6202f70524e7d27b23175a414beed8f": "Paid",
    "0xd0641bc0d0e2888a0a4ecc2fe40280fcfa3d7f0f4049d3a00dc3b7f81501fd2b": "SignerRevoked",
    "0x5dadb9327f31617894c84554ca8e2073b38829ec9d8ebf4a7fdb00f45be25686": "Closed",
}
SIGNATURES = {
    "Offered": "Offered(bytes32,address,(bytes32,uint64,bytes32,address,uint64,address,uint64,uint64,uint64,uint64))",
    "Accepted": "Accepted(bytes32)",
    "Paid": "Paid(bytes32,bytes32,uint64,uint8,bytes32,bytes32)",
    "SignerRevoked": "SignerRevoked(bytes32)",
    "Closed": "Closed(bytes32,uint64)",
}
AUTHENTICATORS = ("Proof", "Signature", "BuyerApproval")
TERMS = ("policyHash", "policyVersion", "poId", "recipient", "maxTotal", "signer",
         "signerAllowance", "proofThreshold", "acceptBy", "settleBy")
TERM_TYPES = ("bytes32", "uint", "bytes32", "address", "uint", "address", "uint", "uint", "uint", "uint")


class Rpc:
    def __init__(self, url, timeout=30):
        self.url, self.timeout = url, timeout

    def call(self, method, params=()):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            response = json.load(r)
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response["result"]

    def block_number(self):
        return int(self.call("eth_blockNumber"), 16)

    def block(self, number):
        b = self.call("eth_getBlockByNumber", [hex(number), False])
        if b is None:
            raise RuntimeError(f"Block {number} not found")
        return {"number": number, "hash": b["hash"], "timestamp": int(b["timestamp"], 16)}

    def logs(self, address, start, end):
        return self.call("eth_getLogs", [{"address": address, "fromBlock": hex(start), "toBlock": hex(end)}])


def words(data):
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(raw) % 32:
        raise ValueError("ABI data is not a whole number of words")
    return [raw[i:i + 32] for i in range(0, len(raw), 32)]


def _value(word, kind):
    if kind == "bytes32":
        return "0x" + word.hex()
    if kind == "address":
        if any(word[:12]):
            raise ValueError("Address word has non-zero padding")
        return "0x" + word[12:].hex()
    return int.from_bytes(word, "big")


def decode(log):
    """Returns (event name, decoded fields), or None for a log of another event."""
    topics = [t.lower() for t in log["topics"]]
    name = TOPICS.get(topics[0]) if topics else None
    if name is None:
        return None
    w = words(log["data"])
    out = {"orderId": topics[1]}
    if name == "Offered":
        if len(w) != 10 or len(topics) != 3:
            raise ValueError("Malformed Offered log")
        out["customer"] = _value(bytes.fromhex(topics[2][2:]), "address")
        out["terms"] = {k: _value(x, t) for k, x, t in zip(TERMS, w, TERM_TYPES)}
    elif name == "Paid":
        if len(w) != 4 or len(topics) != 3:
            raise ValueError("Malformed Paid log")
        out["obligationId"] = topics[2]
        out["amount"] = _value(w[0], "uint")
        out["authenticator"] = AUTHENTICATORS[_value(w[1], "uint")]
        out["documentHash"] = _value(w[2], "bytes32")
        out["evidenceHash"] = _value(w[3], "bytes32")
    elif name == "Closed":
        if len(w) != 1:
            raise ValueError("Malformed Closed log")
        out["refunded"] = _value(w[0], "uint")
    elif w:
        raise ValueError(f"Malformed {name} log")
    return name, out
