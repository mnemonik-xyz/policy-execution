"""Escrow contract interactions, journal encoding, and settlement logic for Warrant.

Uses eth-abi and eth-account for fast, lightweight in-process EVM interaction without cast subprocesses.
"""

import contextlib
import functools
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from .adapter import canonical, digest

__all__ = [
    "DEFAULT_CATEGORY",
    "DEFAULT_CHAIN_ID",
    "MOCK_IMAGE_ID",
    "MOCK_SEAL",
    "REPO_ROOT",
    "EscrowClient",
    "EthRpc",
    "address20",
    "bytes32",
    "canonical",
    "compile_buyer_policy",
    "digest",
    "encode_journal",
    "ensure_policy_hash",
    "evaluate_authorization",
    "find_binary",
    "find_warrant_binary",
    "should_trust_env",
    "sign_acceptance",
    "to_addr_str",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAIN_ID = 31337
DEFAULT_CATEGORY = 42
MOCK_SEAL = "0xabcd"
MOCK_IMAGE_ID = "0x" + "00" * 31 + "01"


def should_trust_env(url_or_hostname: str | None) -> bool:
    """Determine whether HTTP client should trust proxy environment variables."""
    if not url_or_hostname:
        return False
    hostname = urllib.parse.urlsplit(url_or_hostname).hostname if "://" in url_or_hostname else url_or_hostname
    return hostname not in ("127.0.0.1", "localhost", "::1")


def bytes32(val) -> bytes:
    if isinstance(val, (bytes, bytearray)):
        if len(val) == 32:
            return bytes(val)
        raise ValueError(f"bytes32 requires exactly 32 bytes, got {len(val)}")
    if isinstance(val, int):
        return val.to_bytes(32, "big")
    s = str(val).strip()
    if s.startswith("0x"):
        s = s[2:]
    raw = bytes.fromhex(s)
    if len(raw) != 32:
        raise ValueError(f"bytes32 requires exactly 32 bytes (64 hex characters), got {len(raw)} ({s})")
    return raw


def address20(val) -> bytes:
    if isinstance(val, (bytes, bytearray)):
        if len(val) == 20:
            return bytes(val)
        raise ValueError(f"address20 requires exactly 20 bytes, got {len(val)}")
    s = str(val).strip()
    if s.startswith("0x"):
        s = s[2:]
    raw = bytes.fromhex(s)
    if len(raw) != 20:
        raise ValueError(f"address20 requires exactly 20 bytes (40 hex characters), got {len(raw)} ({s})")
    return raw


def to_addr_str(val) -> str:
    if isinstance(val, (bytes, bytearray)):
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
        self._chain_id: int | None = None

    def rpc(self, method: str, params: list | None = None) -> any:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.rpc_url, payload, {"Content-Type": "application/json"})
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if not should_trust_env(self.rpc_url)
            else urllib.request.build_opener()
        )
        with opener.open(req, timeout=15) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"RPC error {method}: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"RPC response missing result for {method}")
        return data["result"]

    def chain_id(self) -> int:
        if self._chain_id is None:
            res = self.rpc("eth_chainId")
            self._chain_id = int(res, 16)
        return self._chain_id

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


def find_warrant_binary(name: str) -> Path:
    for cand in [
        REPO_ROOT / "target" / "release" / name,
        REPO_ROOT / "target" / "debug" / name,
        Path(shutil.which(name) or ""),
    ]:
        if cand and cand.exists() and cand.is_file():
            return cand
    raise FileNotFoundError(f"Could not find compiled binary '{name}'. Run 'cargo build --release -p warrant-host'.")


def compile_buyer_policy(
    template_path: Path | None = None,
    manifest_path: Path | None = None,
    chain_id: int | None = None,
    escrow_address: str | None = None,
    token_address: str | None = None,
    max_amount: int | None = None,
    valid_after_buffer: int = 300,
    validity_seconds: int = 900,
) -> tuple[dict, str]:
    """Compile buyer policy template into concrete Policy and compute its cryptographic policy_hash."""
    manifest_path = manifest_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "manifest.yaml")
    template_path = template_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml")

    manifest = yaml.safe_load(manifest_path.read_text()) if manifest_path.exists() else {}
    template = yaml.safe_load(template_path.read_text()) if template_path.exists() else {}

    chain_id = chain_id or manifest.get("chain_id", DEFAULT_CHAIN_ID)
    escrow_address = escrow_address or manifest.get("escrow_address")
    token_address = token_address or manifest.get("token_address")
    max_amount = max_amount or manifest.get("max_amount", 2_000_000)
    validity_seconds = int(manifest.get("validity_seconds", validity_seconds))

    if not escrow_address or not token_address:
        raise ValueError("escrow_address and token_address must be specified")

    evidence_bin = find_warrant_binary("warrant-evidence")
    policy_bin = find_warrant_binary("warrant-policy")

    reg_key_rel = manifest.get("private_key_files", {}).get("registry_key", "showcases/ionet_arc/.keys/registry.key")
    acc_key_rel = manifest.get("private_key_files", {}).get(
        "buyer_acceptance_key", "showcases/ionet_arc/.keys/buyer_acceptance.key"
    )

    reg_key_path = REPO_ROOT / reg_key_rel
    acc_key_path = REPO_ROOT / acc_key_rel

    if not reg_key_path.exists() or not acc_key_path.exists():
        raise FileNotFoundError(f"Authority keys missing: {reg_key_path} or {acc_key_path}")

    reg_pub = json.loads(
        subprocess.run(
            [str(evidence_bin), "public-key", str(reg_key_path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    acc_pub = json.loads(
        subprocess.run(
            [str(evidence_bin), "public-key", str(acc_key_path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    now = int(time.time())
    valid_after = now - valid_after_buffer
    valid_until = now + validity_seconds

    category = int(manifest.get("category", DEFAULT_CATEGORY))
    rule = template.get("rule") or {
        "all": [
            "accepted",
            {"amount_at_most": int(max_amount)},
            {"vendor_category_in": [category]},
        ]
    }

    policy = {
        "version": int(template.get("version", 1)),
        "scope": {
            "chain_id": int(chain_id),
            "vault": list(address20(escrow_address)),
            "token": list(address20(token_address)),
        },
        "valid_after": valid_after,
        "valid_until": valid_until,
        "registry_key": reg_pub,
        "acceptance_key": acc_pub,
        "rule": rule,
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(policy, f)
        temp_file = Path(f.name)

    try:
        res = subprocess.run([str(policy_bin), "hash", str(temp_file)], capture_output=True, text=True, check=True)
        policy_hash = res.stdout.strip()
    finally:
        temp_file.unlink(missing_ok=True)

    return policy, policy_hash


def sign_acceptance(acceptance: dict, key_path: Path | None = None) -> list[int]:
    """Sign Acceptance struct with buyer_acceptance.key using warrant-evidence."""
    key_path = key_path or (REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "buyer_acceptance.key")
    if not key_path.exists():
        raise FileNotFoundError(f"Buyer acceptance key not found at {key_path}")

    evidence_bin = find_warrant_binary("warrant-evidence")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        acc_file = tmp / "statement.json"
        sig_file = tmp / "signature.json"
        acc_file.write_text(json.dumps(acceptance))

        subprocess.run(
            [str(evidence_bin), "sign-acceptance", str(key_path), str(acc_file), str(sig_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(sig_file.read_text().strip())


def sign_vendor_credential(credential: dict, key_path: Path | None = None) -> list[int]:
    """Sign VendorCredential struct with registry.key using warrant-evidence."""
    key_path = key_path or (REPO_ROOT / "showcases" / "ionet_arc" / ".keys" / "registry.key")
    if not key_path.exists():
        raise FileNotFoundError(f"Registry authority key not found at {key_path}")

    evidence_bin = find_warrant_binary("warrant-evidence")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        cred_file = tmp / "credential.json"
        sig_file = tmp / "signature.json"
        cred_file.write_text(json.dumps(credential))

        subprocess.run(
            [str(evidence_bin), "sign-vendor", str(key_path), str(cred_file), str(sig_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(sig_file.read_text().strip())


def evaluate_authorization(policy: dict, request: dict, evidence: dict) -> tuple[dict, bytes]:
    """Execute native warrant-host evaluate on Input (policy + request + evidence).

    Returns (auth_dict, journal_bytes).
    """
    host_bin = find_warrant_binary("warrant-host")
    input_obj = {
        "policy": policy,
        "request": request,
        "evidence": evidence,
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        input_file = tmp / "input.json"
        input_file.write_text(json.dumps(input_obj))

        res = subprocess.run(
            [str(host_bin), "evaluate", str(input_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        auth = json.loads(res.stdout.strip())

    journal_bytes = encode_journal(
        policy_hash=bytes(auth["policy_hash"]),
        chain_id=auth["request"]["scope"]["chain_id"],
        vault=bytes(auth["request"]["scope"]["vault"]),
        token=bytes(auth["request"]["scope"]["token"]),
        recipient=bytes(auth["request"]["recipient"]),
        amount=auth["request"]["amount"],
        task_id=bytes(auth["request"]["task_id"]),
        deliverable_hash=bytes(auth["request"]["deliverable_hash"]),
        policy_version=auth["policy_version"],
        valid_after=auth["valid_after"],
        valid_until=auth["valid_until"],
        evidence_hash=bytes(auth["evidence_hash"]),
    )
    return auth, journal_bytes


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
            candidate_pk = os.environ.get("WARRANT_PRIVATE_KEY")
            acc = Account.from_key(candidate_pk)
            if acc.address.lower() == from_addr.lower():
                pk = candidate_pk

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

    def approve_journal(self, journal_bytes: bytes, sender: str | None = None) -> dict:
        """Call JournalVerifier.approve(journal)."""
        sender = sender or self.server_account
        if not sender:
            raise ValueError("Sender required to approve journal")
        if not self.verifier:
            raise ValueError("Verifier address required to approve journal")
        selector = function_signature_to_4byte_selector("approve(bytes)")
        calldata = selector + encode(["bytes"], [journal_bytes])
        return self.send_transaction(sender, self.verifier, calldata)

    def settle(self, seal: bytes | str, journal_bytes: bytes, sender: str | None = None) -> dict:
        """Call TaskEscrow.settle(seal, journal)."""
        sender = sender or self.server_account
        if not sender:
            raise ValueError("Sender required to settle task")
        seal_bytes = (
            bytes.fromhex(seal[2:] if str(seal).startswith("0x") else str(seal))
            if isinstance(seal, str)
            else bytes(seal)
        )
        selector = function_signature_to_4byte_selector("settle(bytes,bytes)")
        calldata = selector + encode(["bytes", "bytes"], [seal_bytes, journal_bytes])
        return self.send_transaction(sender, self.escrow, calldata)

    def settle_mock(
        self,
        journal_bytes: bytes,
        sender: str | None = None,
        seal_hex: str = MOCK_SEAL,
    ) -> dict:
        """Approve journal on JournalVerifier (if configured) and settle TaskEscrow with mock seal."""
        sender = sender or self.server_account
        if self.verifier:
            with contextlib.suppress(Exception):
                self.approve_journal(journal_bytes, sender=sender)
        return self.settle(seal_hex, journal_bytes, sender=sender)

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
    state_dir: Path | None = None,
    categories: list[int] | None = None,
    valid_after: int = 1000,
    valid_until: int = 2_000_000_000,
) -> str:
    """Instantiate and hash policy for this scope."""
    manifest_path = REPO_ROOT / "showcases" / "ionet_arc" / "config" / "manifest.yaml"
    template_path = REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml"
    if manifest_path.exists() and template_path.exists():
        try:
            _, pol_hash = compile_buyer_policy(
                template_path=template_path,
                manifest_path=manifest_path,
                chain_id=chain_id,
                escrow_address=escrow_address,
                token_address=token_address,
                max_amount=max_amount,
            )
            return pol_hash
        except Exception:
            pass

    # Fallback to templates/example-parameters.json if config doesn't exist
    params_template = REPO_ROOT / "templates" / "example-parameters.json"
    template_file = REPO_ROOT / "templates" / "accepted-contractor-v1.json"
    policy_binary = find_warrant_binary("warrant-policy")

    escrow_bytes = list(address20(escrow_address))
    token_bytes = list(address20(token_address))

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

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        params_path = tmp / "params.json"
        policy_path = tmp / "policy.json"
        params_path.write_text(json.dumps(params_obj, indent=2))
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
