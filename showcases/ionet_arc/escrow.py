"""Escrow contract interactions, journal encoding, and settlement logic for Warrant.

Uses eth-abi and eth-account for fast, lightweight in-process EVM interaction without cast subprocesses.
"""
import functools
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAIN_ID = 31337
DEFAULT_CATEGORY = 100
MOCK_SEAL = "0xabcd"
MOCK_IMAGE_ID = "0x" + "00" * 31 + "01"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def bytes32(val) -> bytes:
    if isinstance(val, bytes):
        return val.rjust(32, b"\x00") if len(val) < 32 else val[:32]
    s = str(val).strip()
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s.zfill(64))


def address20(val) -> bytes:
    if isinstance(val, bytes):
        return val.rjust(20, b"\x00") if len(val) < 20 else val[:20]
    s = str(val).strip()
    if s.startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s.zfill(40))


def to_addr_str(val) -> str:
    if isinstance(val, bytes):
        return to_checksum_address("0x" + val[-20:].hex())
    s = str(val).strip()
    if not s.startswith("0x"):
        s = "0x" + s
    return to_checksum_address(s)


def encode_journal(
    policy_hash: str | bytes,
    chain_id: int,
    vault: str | bytes,
    token: str | bytes,
    recipient: str | bytes,
    amount: int,
    task_id: str | bytes,
    deliverable_hash: str | bytes,
    policy_version: int,
    valid_after: int,
    valid_until: int,
    evidence_hash: str | bytes,
) -> bytes:
    """Encode exactly twelve 32-byte words (384 bytes) matching PolicyExecutionVault.Authorization."""
    types = [
        "bytes32",
        "uint64",
        "address",
        "address",
        "address",
        "uint64",
        "bytes32",
        "bytes32",
        "uint64",
        "uint64",
        "uint64",
        "bytes32",
    ]
    vals = [
        bytes32(policy_hash),
        int(chain_id),
        to_addr_str(vault),
        to_addr_str(token),
        to_addr_str(recipient),
        int(amount),
        bytes32(task_id),
        bytes32(deliverable_hash),
        int(policy_version),
        int(valid_after),
        int(valid_until),
        bytes32(evidence_hash),
    ]
    return encode(types, vals)


class EthRpc:
    """Lightweight JSON-RPC client."""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def rpc(self, method: str, params: list | None = None) -> any:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.rpc_url, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"RPC error {method}: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"RPC response missing result for {method}")
        return data["result"]

    def chain_id(self) -> int:
        res = self.rpc("eth_chainId")
        return int(res, 16)

    def accounts(self) -> list[str]:
        return self.rpc("eth_accounts")

    def latest_timestamp(self) -> int:
        block = self.rpc("eth_getBlockByNumber", ["latest", False])
        return int(block["timestamp"], 16)


@functools.lru_cache(maxsize=16)
def find_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".foundry" / "bin" / name
    if fallback.exists():
        return str(fallback)
    return name


class EscrowClient:
    """Interface to query and settle TaskEscrow on-chain using eth-abi and eth-account."""

    STATES = ("Missing", "Offered", "Accepted", "Paid", "Refunded")

    def __init__(
        self,
        rpc_url: str = "http://127.0.0.1:8545",
        escrow_address: str | None = None,
        token_address: str | None = None,
        verifier_address: str | None = None,
        server_account: str | None = None,
        private_keys: dict[str, str] | None = None,
    ):
        self.rpc_url = rpc_url
        self.rpc = EthRpc(rpc_url)
        self.escrow = escrow_address
        self.token = token_address
        self.verifier = verifier_address
        self.server_account = server_account
        self.private_keys = {k.lower(): v for k, v in (private_keys or {}).items()}

    def register_key(self, address: str, private_key: str):
        self.private_keys[address.lower()] = private_key

    def call_contract(
        self,
        to_address: str,
        signature: str,
        types: list,
        args: list,
        output_types: list,
    ) -> tuple:
        selector = function_signature_to_4byte_selector(signature)
        calldata = selector + encode(types, args)
        target = to_checksum_address(to_address)
        raw_res = self.rpc.rpc("eth_call", [{"to": target, "data": "0x" + calldata.hex()}, "latest"])
        if not raw_res or raw_res == "0x":
            raise RuntimeError(f"Contract call to {signature} returned empty response")
        return decode(output_types, bytes.fromhex(raw_res[2:]))

    def send_transaction(self, from_account: str, to_address: str, calldata: bytes) -> dict:
        to_addr = to_checksum_address(to_address)
        from_addr = to_checksum_address(from_account)
        pk = self.private_keys.get(from_addr.lower())
        if not pk and os.environ.get("WARRANT_PRIVATE_KEY"):
            pk = os.environ.get("WARRANT_PRIVATE_KEY")

        if pk:
            acc = Account.from_key(pk)
            nonce = int(self.rpc.rpc("eth_getTransactionCount", [acc.address, "pending"]), 16)
            chain_id = int(self.rpc.chain_id())
            gas_price = int(self.rpc.rpc("eth_gasPrice", []), 16)
            try:
                gas_est = int(
                    self.rpc.rpc(
                        "eth_estimateGas",
                        [{"from": acc.address, "to": to_addr, "data": "0x" + calldata.hex()}],
                    ),
                    16,
                )
                gas = int(gas_est * 1.2)
            except Exception:
                gas = 500_000

            tx = {
                "to": to_addr,
                "value": 0,
                "data": calldata,
                "nonce": nonce,
                "chainId": chain_id,
                "gas": gas,
                "gasPrice": gas_price,
            }
            signed = acc.sign_transaction(tx)
            tx_hash = self.rpc.rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        else:
            # Unlocked account (e.g. local Anvil)
            tx_params = {
                "from": from_addr,
                "to": to_addr,
                "data": "0x" + calldata.hex(),
            }
            tx_hash = self.rpc.rpc("eth_sendTransaction", [tx_params])

        receipt = self.wait_for_receipt(tx_hash)
        status = receipt.get("status")
        if status is not None and int(str(status), 16) != 1:
            raise RuntimeError(f"Transaction reverted: {receipt}")
        return receipt

    def wait_for_receipt(self, tx_hash: str, timeout: float = 30.0) -> dict:
        started = time.time()
        while time.time() - started < timeout:
            receipt = self.rpc.rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(0.05)
        raise TimeoutError(f"Transaction receipt timeout for {tx_hash}")

    def task_id_for(self, customer: str, salt: str) -> str:
        """Call TaskEscrow.taskIdFor(customer, salt)."""
        res = self.call_contract(
            self.escrow,
            "taskIdFor(address,bytes32)",
            ["address", "bytes32"],
            [to_checksum_address(customer), bytes32(salt)],
            ["bytes32"],
        )
        return "0x" + res[0].hex()

    def get_task(self, task_id: str) -> dict:
        """Read Task struct from tasks(taskId)."""
        res = self.call_contract(
            self.escrow,
            "tasks(bytes32)",
            ["bytes32"],
            [bytes32(task_id)],
            ["address", "address", "bytes32", "uint64", "uint64", "uint64", "uint64", "uint8"],
        )
        customer, recipient, policy_hash, policy_version, amount, accept_by, settle_by, state_idx = res
        state_name = self.STATES[state_idx] if state_idx < len(self.STATES) else "Unknown"

        return {
            "task_id": task_id,
            "customer": to_checksum_address(customer),
            "recipient": to_checksum_address(recipient),
            "policy_hash": "0x" + policy_hash.hex(),
            "policy_version": policy_version,
            "amount": amount,
            "accept_by": accept_by,
            "settle_by": settle_by,
            "state_idx": state_idx,
            "state": state_name,
        }

    def accept(self, task_id: str, sender: str | None = None) -> dict:
        sender = sender or self.server_account
        if not sender:
            raise ValueError("Sender required to accept task")
        selector = function_signature_to_4byte_selector("accept(bytes32)")
        calldata = selector + encode(["bytes32"], [bytes32(task_id)])
        return self.send_transaction(sender, self.escrow, calldata)

    def settle_mock(
        self,
        journal_bytes: bytes,
        sender: str | None = None,
        seal_hex: str = MOCK_SEAL,
    ) -> dict:
        """Approve journal on JournalVerifier and settle TaskEscrow with mock seal."""
        sender = sender or self.server_account
        # Approve on verifier
        if self.verifier:
            selector_approve = function_signature_to_4byte_selector("approve(bytes)")
            calldata_approve = selector_approve + encode(["bytes"], [journal_bytes])
            self.send_transaction(sender, self.verifier, calldata_approve)
        # Settle on escrow
        seal = bytes.fromhex(seal_hex[2:] if seal_hex.startswith("0x") else seal_hex)
        selector_settle = function_signature_to_4byte_selector("settle(bytes,bytes)")
        calldata_settle = selector_settle + encode(["bytes", "bytes"], [seal, journal_bytes])
        return self.send_transaction(sender, self.escrow, calldata_settle)

    def token_balance(self, account: str) -> int:
        """Read ERC-20 token balance for account."""
        res = self.call_contract(
            self.token,
            "balanceOf(address)",
            ["address"],
            [to_checksum_address(account)],
            ["uint256"],
        )
        return res[0]

    def approve_token(self, spender: str, amount: int, sender: str) -> dict:
        """Approve spender allowance on ERC-20 token."""
        selector = function_signature_to_4byte_selector("approve(address,uint256)")
        calldata = selector + encode(["address", "uint256"], [to_checksum_address(spender), int(amount)])
        return self.send_transaction(sender, self.token, calldata)

    def mint_token(self, account: str, amount: int, sender: str | None = None) -> dict:
        """Mint test tokens to account (TestToken on local Anvil / testnet)."""
        sender = sender or account
        selector = function_signature_to_4byte_selector("mint(address,uint256)")
        calldata = selector + encode(["address", "uint256"], [to_checksum_address(account), int(amount)])
        return self.send_transaction(sender, self.token, calldata)

    def offer(
        self,
        salt: str,
        recipient: str,
        policy_hash: str,
        policy_version: int,
        amount: int,
        accept_by: int,
        settle_by: int,
        sender: str,
    ) -> dict:
        """Submit TaskEscrow.offer() on-chain."""
        selector = function_signature_to_4byte_selector("offer(bytes32,address,bytes32,uint64,uint64,uint64,uint64)")
        calldata = selector + encode(
            ["bytes32", "address", "bytes32", "uint64", "uint64", "uint64", "uint64"],
            [
                bytes32(salt),
                to_checksum_address(recipient),
                bytes32(policy_hash),
                int(policy_version),
                int(amount),
                int(accept_by),
                int(settle_by),
            ],
        )
        return self.send_transaction(sender, self.escrow, calldata)

    def deploy_local_anvil(self, customer: str, agent: str) -> dict:
        """Deploy TestToken, JournalVerifier, and TaskEscrow to local anvil node."""
        contracts_dir = REPO_ROOT / "contracts"

        def deploy_contract(name, *args):
            cmd = [
                find_binary("forge"),
                "create",
                name,
                "--broadcast",
                "--unlocked",
                "--from",
                customer,
                "--rpc-url",
                self.rpc_url,
                "--json",
            ]
            if args:
                cmd += ["--constructor-args", *[str(a) for a in args]]
            res = subprocess.run(cmd, cwd=contracts_dir, text=True, capture_output=True)
            if res.returncode != 0:
                raise RuntimeError(f"Deploy {name} failed: {res.stderr.strip() or res.stdout.strip()}")
            return json.loads(res.stdout)["deployedTo"]

        token = deploy_contract("test/PolicyExecutionVault.t.sol:TestToken")
        verifier = deploy_contract("test/PolicyExecutionVault.t.sol:JournalVerifier")
        escrow = deploy_contract("src/TaskEscrow.sol:TaskEscrow", token, verifier, MOCK_IMAGE_ID)

        self.token = token
        self.verifier = verifier
        self.escrow = escrow
        self.server_account = agent

        # Mint and approve test tokens for customer
        self.mint_token(customer, 100_000_000, sender=customer)
        self.approve_token(escrow, 100_000_000, sender=customer)

        return {
            "token": token,
            "verifier": verifier,
            "escrow": escrow,
            "customer": customer,
            "agent": agent,
            "chain_id": self.rpc.chain_id(),
        }


def ensure_policy_hash(
    escrow_address: str,
    token_address: str,
    chain_id: int,
    max_amount: int,
    state_dir: Path,
    categories: list[int] | None = None,
    valid_after: int = 1000,
    valid_until: int = 2_000_000_000,
) -> str:
    """Instantiate and hash accepted-contractor-v1 policy for this scope."""
    params_template = REPO_ROOT / "templates" / "example-parameters.json"
    template_file = REPO_ROOT / "templates" / "accepted-contractor-v1.json"
    policy_binary = None
    for cand in [
        REPO_ROOT / "target" / "release" / "warrant-policy",
        REPO_ROOT / "target" / "debug" / "warrant-policy",
        Path(shutil.which("warrant-policy") or ""),
    ]:
        if cand and cand.exists() and cand.is_file():
            policy_binary = cand
            break

    escrow_bytes = list(bytes.fromhex(escrow_address[2:] if escrow_address.startswith("0x") else escrow_address))
    token_bytes = list(bytes.fromhex(token_address[2:] if token_address.startswith("0x") else token_address))

    params_obj = json.loads(params_template.read_text())
    params_obj["policy"]["scope"] = {
        "chain_id": chain_id,
        "vault": escrow_bytes,
        "token": token_bytes,
    }
    params_obj["policy"]["valid_after"] = valid_after
    params_obj["policy"]["valid_until"] = valid_until
    params_obj["bindings"] = {
        "max_amount": max_amount,
        "categories": categories or [DEFAULT_CATEGORY],
    }

    state_dir.mkdir(parents=True, exist_ok=True)
    params_path = state_dir / "policy_parameters.json"
    policy_path = state_dir / "policy.json"
    params_path.write_text(json.dumps(params_obj, indent=2))

    if policy_binary and template_file.exists():
        if policy_path.exists():
            policy_path.unlink()
        subprocess.run(
            [str(policy_binary), "instantiate", str(template_file), str(params_path), str(policy_path)],
            check=True,
            capture_output=True,
        )
        res = subprocess.run(
            [str(policy_binary), "hash", str(policy_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        return res.stdout.strip()
    return "0x" + hashlib.sha256(canonical(params_obj)).hexdigest()
