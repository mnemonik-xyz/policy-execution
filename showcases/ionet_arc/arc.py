"""Arc network profiles and read-only checks; no signing or asset transfers."""
import json
import urllib.request

from .adapter import PilotError

USDC = "0x3600000000000000000000000000000000000000"
NETWORKS = {
    "testnet": {"chain_id": 5042002, "rpc": "https://rpc.testnet.arc.io", "cctp_domain": 26},
    "mainnet": {"chain_id": 5042, "rpc": "https://rpc.mainnet.arc.io", "cctp_domain": 26},
}


def rpc(url, method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if "error" in data or "result" not in data:
        raise PilotError("Arc RPC request failed")
    return data["result"]


def preflight(network, rpc_url=None, call=rpc):
    profile = NETWORKS[network]
    url = rpc_url or profile["rpc"]
    if int(call(url, "eth_chainId", []), 16) != profile["chain_id"]:
        raise PilotError("RPC chain ID does not match the selected Arc network")
    decimals = int(call(url, "eth_call", [{"to": USDC, "data": "0x313ce567"}, "latest"]), 16)
    if decimals != 6:
        raise PilotError("Unexpected Arc USDC ERC-20 decimals")
    return {"network": network, "chain_id": profile["chain_id"], "usdc": USDC,
            "erc20_decimals": decimals, "native_decimals": 18,
            "cctp_domain": 26, "read_only": True,
            "verifier_checked": False, "escrow_deployed": False}
