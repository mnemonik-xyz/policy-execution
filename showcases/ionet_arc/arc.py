"""Arc network profiles and read-only checks; no signing or asset transfers."""
from .adapter import PilotError
from .escrow import EthRpc

USDC = "0x3600000000000000000000000000000000000000"
NETWORKS = {
    "testnet": {"chain_id": 5042002, "rpc": "https://rpc.testnet.arc.io", "cctp_domain": 26},
    "mainnet": {"chain_id": 5042, "rpc": "https://rpc.mainnet.arc.io", "cctp_domain": 26},
}


def rpc(url, method, params):
    try:
        return EthRpc(url).rpc(method, params)
    except Exception as exc:
        raise PilotError(f"Arc RPC request failed: {exc}") from exc


def preflight(network, rpc_url=None, call=rpc):
    profile = NETWORKS[network]
    url = rpc_url or profile["rpc"]
    if int(call(url, "eth_chainId", []), 16) != profile["chain_id"]:
        raise PilotError("RPC chain ID does not match the selected Arc network")
    decimals = int(call(url, "eth_call", [{"to": USDC, "data": "0x313ce567"}, "latest"]), 16)
    if decimals != 6:
        raise PilotError("Unexpected Arc USDC ERC-20 decimals")
    return {
        "network": network,
        "chain_id": profile["chain_id"],
        "usdc": USDC,
        "erc20_decimals": decimals,
        "native_decimals": 18,
        "cctp_domain": 26,
        "read_only": True,
        "verifier_checked": False,
        "escrow_deployed": False,
    }
