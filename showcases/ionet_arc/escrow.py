"""Escrow contract interactions, journal encoding, and settlement logic for Warrant."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request

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
    out = bytearray(384)
    # word 0: policyHash
    out[0:32] = bytes32(policy_hash)
    # word 1: chainId (uint64)
    out[32:64] = chain_id.to_bytes(32, "big")
    # word 2: vault (address)
    out[64:96] = b"\x00" * 12 + address20(vault)
    # word 3: token (address)
    out[96:128] = b"\x00" * 12 + address20(token)
    # word 4: recipient (address)
    out[128:160] = b"\x00" * 12 + address20(recipient)
    # word 5: amount (uint64)
    out[160:192] = int(amount).to_bytes(32, "big")
    # word 6: taskId (bytes32)
    out[192:224] = bytes32(task_id)
    # word 7: deliverableHash (bytes32)
    out[224:256] = bytes32(deliverable_hash)
    # word 8: policyVersion (uint64)
    out[256:288] = int(policy_version).to_bytes(32, "big")
    # word 9: validAfter (uint64)
    out[288:320] = int(valid_after).to_bytes(32, "big")
    # word 10: validUntil (uint64)
    out[320:352] = int(valid_until).to_bytes(32, "big")
    # word 11: evidenceHash (bytes32)
    out[352:384] = bytes32(evidence_hash)
    return bytes(out)


class EthRpc:
    """Lightweight JSON-RPC client."""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    def rpc(self, method: str, params: list = None):
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.rpc_url, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"RPC error {method}: {data['error']}")
        return data["result"]

    def chain_id(self) -> int:
        res = self.rpc("eth_chainId")
        return int(res, 16)

    def accounts(self) -> list[str]:
        return self.rpc("eth_accounts")

    def latest_timestamp(self) -> int:
        block = self.rpc("eth_getBlockByNumber", ["latest", False])
        return int(block["timestamp"], 16)


def find_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".foundry" / "bin" / name
    if fallback.exists():
        return str(fallback)
    return name


class EscrowClient:
    """Interface to query and settle TaskEscrow on-chain."""

    STATES = ["Missing", "Offered", "Accepted", "Paid", "Refunded"]

    def __init__(
        self,
        rpc_url: str = "http://127.0.0.1:8545",
        escrow_address: str | None = None,
        token_address: str | None = None,
        verifier_address: str | None = None,
        server_account: str | None = None,
    ):
        self.rpc_url = rpc_url
        self.rpc = EthRpc(rpc_url)
        self.escrow = escrow_address
        self.token = token_address
        self.verifier = verifier_address
        self.server_account = server_account

    def call_cast(self, *args, cwd: Path = REPO_ROOT) -> str:
        env = dict(os.environ)
        cmd = [find_binary("cast"), *[str(a) for a in args], "--rpc-url", self.rpc_url]
        res = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"cast {' '.join(str(a) for a in args)} failed: {err}")
        return res.stdout.strip()

    def send_cast(self, from_account: str, *args, cwd: Path = REPO_ROOT) -> dict:
        env = dict(os.environ)
        cmd = [
            find_binary("cast"),
            "send",
            *[str(a) for a in args],
            "--from",
            from_account,
            "--unlocked",
            "--rpc-url",
            self.rpc_url,
            "--json",
        ]
        res = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
        if res.returncode != 0:
            raise RuntimeError(f"cast send failed: {res.stderr.strip() or res.stdout.strip()}")
        receipt = json.loads(res.stdout)
        if int(receipt.get("status", "0"), 16) != 1:
            raise RuntimeError(f"Transaction reverted: {receipt}")
        return receipt

    def task_id_for(self, customer: str, salt: str) -> str:
        """Call TaskEscrow.taskIdFor(customer, salt)."""
        res = self.call_cast("call", self.escrow, "taskIdFor(address,bytes32)(bytes32)", customer, salt)
        return res.split()[0]

    def get_task(self, task_id: str) -> dict:
        """Read Task struct from tasks(taskId)."""
        res = self.call_cast(
            "call",
            self.escrow,
            "tasks(bytes32)(address,address,bytes32,uint64,uint64,uint64,uint64,uint8)",
            task_id,
        )
        lines = [line.strip() for line in res.splitlines() if line.strip()]
        if len(lines) < 8:
            parts = res.split()
            if len(parts) >= 8:
                lines = parts
            else:
                raise RuntimeError(f"Unexpected tasks output format: {res}")

        def _parse_uint(val) -> int:
            if isinstance(val, int):
                return val
            return int(str(val).split()[0].split("[")[0].strip())

        customer = lines[0].split()[0]
        recipient = lines[1].split()[0]
        policy_hash = lines[2].split()[0]
        policy_version = _parse_uint(lines[3])
        amount = _parse_uint(lines[4])
        accept_by = _parse_uint(lines[5])
        settle_by = _parse_uint(lines[6])
        state_idx = _parse_uint(lines[7])
        state_name = self.STATES[state_idx] if state_idx < len(self.STATES) else "Unknown"

        return {
            "task_id": task_id,
            "customer": customer,
            "recipient": recipient,
            "policy_hash": policy_hash,
            "policy_version": policy_version,
            "amount": amount,
            "accept_by": accept_by,
            "settle_by": settle_by,
            "state_idx": state_idx,
            "state": state_name,
        }

    def accept(self, task_id: str, sender: str = None) -> dict:
        sender = sender or self.server_account
        if not sender:
            raise ValueError("Sender required to accept task")
        return self.send_cast(sender, self.escrow, "accept(bytes32)", task_id)

    def settle_mock(
        self,
        journal_bytes: bytes,
        sender: str = None,
        seal_hex: str = MOCK_SEAL,
    ) -> dict:
        """Approve journal on JournalVerifier and settle TaskEscrow with mock seal."""
        sender = sender or self.server_account
        journal_hex = "0x" + journal_bytes.hex()
        # Approve on verifier
        if self.verifier:
            self.send_cast(sender, self.verifier, "approve(bytes)", journal_hex)
        # Settle on escrow
        return self.send_cast(sender, self.escrow, "settle(bytes,bytes)", seal_hex, journal_hex)

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
        self.send_cast(customer, token, "mint(address,uint256)", customer, 100_000_000)
        self.send_cast(customer, token, "approve(address,uint256)", escrow, 100_000_000)

        return {
            "token": token,
            "verifier": verifier,
            "escrow": escrow,
            "customer": customer,
            "agent": agent,
            "chain_id": self.rpc.chain_id(),
        }
