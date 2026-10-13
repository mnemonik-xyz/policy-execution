"""Escrow contract interactions, journal encoding, and settlement logic for Warrant.

Uses eth-abi and eth-account for fast, lightweight in-process EVM interaction without cast subprocesses.
"""

import asyncio
import contextlib
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

import yaml
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import function_signature_to_4byte_selector, keccak, to_checksum_address

from .adapter import canonical, digest

__all__ = [
    "DEFAULT_CATEGORY",
    "DEFAULT_CHAIN_ID",
    "DEFAULT_MANIFEST_PATH",
    "MAINNET_CHAIN_IDS",
    "MOCK_IMAGE_ID",
    "MOCK_SEAL",
    "REPO_ROOT",
    "EscrowClient",
    "EthRpc",
    "address20",
    "bytes32",
    "canonical",
    "compile_buyer_policy",
    "compute_task_id",
    "digest",
    "encode_journal",
    "ensure_policy_hash",
    "evaluate_authorization",
    "find_binary",
    "find_warrant_binary",
    "get_manifest_parameter",
    "hash_policy",
    "load_manifest",
    "parse_public_key",
    "prove_authorization",
    "read_private_key_file",
    "should_trust_env",
    "sign_acceptance",
    "sign_vendor_credential",
    "to_addr_str",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAIN_ID = 31337
DEFAULT_CATEGORY = 42
MOCK_SEAL = "0xabcd"
MOCK_IMAGE_ID = "0x" + "00" * 31 + "01"
MAINNET_CHAIN_IDS = frozenset({5042})  # Arc mainnet. Mock settlement is refused on these chains.
DEFAULT_MANIFEST_PATH = REPO_ROOT / "showcases" / "ionet_arc" / "config" / "manifest.yaml"


def load_manifest(manifest_path: Path | str | None = None) -> dict:
    """Load configuration dictionary from manifest.yaml."""
    path = Path(manifest_path) if manifest_path else DEFAULT_MANIFEST_PATH
    if not path.is_absolute():
        if (REPO_ROOT / path).exists():
            path = REPO_ROOT / path
        elif (Path.cwd() / path).exists():
            path = Path.cwd() / path
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def get_manifest_parameter(manifest: dict, *keys: str):
    """Retrieve first matching parameter from manifest supporting snake_case and kebab-case."""
    for k in keys:
        if k in manifest and manifest[k] is not None:
            return manifest[k]
        k_alt = k.replace("_", "-") if "_" in k else k.replace("-", "_")
        if k_alt in manifest and manifest[k_alt] is not None:
            return manifest[k_alt]
    return None


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


_CHAIN_ID_CACHE: dict[str, int] = {}


class EthRpc:
    """Lightweight JSON-RPC client with static query caching and non-blocking helpers."""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self._chain_id: int | None = _CHAIN_ID_CACHE.get(rpc_url)
        self._accounts: list[str] | None = None
        self._opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if not should_trust_env(self.rpc_url)
            else urllib.request.build_opener()
        )

    def rpc(self, method: str, params: list | None = None) -> any:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; WarrantClient/1.0; +https://mnemonik.xyz)",
        }
        req = urllib.request.Request(self.rpc_url, payload, headers)
        try:
            with self._opener.open(req, timeout=15) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as err:
            body = ""
            with contextlib.suppress(Exception):
                body = err.read().decode("utf-8", errors="replace").strip()
            msg = f"RPC HTTP {err.code} ({err.reason}) calling {method} on {self.rpc_url}"
            if body:
                msg += f": {body}"
            raise RuntimeError(msg) from err
        if "error" in data:
            raise RuntimeError(f"RPC error {method}: {data['error']}")
        if "result" not in data:
            raise RuntimeError(f"RPC response missing result for {method}")
        return data["result"]

    async def async_rpc(self, method: str, params: list | None = None) -> any:
        """Execute RPC query in background thread to prevent blocking the event loop."""
        return await asyncio.to_thread(self.rpc, method, params)

    def chain_id(self) -> int:
        if self._chain_id is None:
            if self.rpc_url in _CHAIN_ID_CACHE:
                self._chain_id = _CHAIN_ID_CACHE[self.rpc_url]
            else:
                res = self.rpc("eth_chainId")
                self._chain_id = int(res, 16)
                _CHAIN_ID_CACHE[self.rpc_url] = self._chain_id
        return self._chain_id

    async def async_chain_id(self) -> int:
        """Asynchronously retrieve or query chain_id without blocking event loop."""
        return await asyncio.to_thread(self.chain_id)

    def accounts(self, use_cache: bool = True) -> list[str]:
        if not use_cache or self._accounts is None:
            self._accounts = self.rpc("eth_accounts")
        return list(self._accounts)

    async def async_accounts(self, use_cache: bool = True) -> list[str]:
        """Asynchronously query accounts without blocking event loop."""
        return await asyncio.to_thread(self.accounts, use_cache)

    def latest_timestamp(self) -> int:
        block = self.rpc("eth_getBlockByNumber", ["latest", False])
        return int(block["timestamp"], 16)

    async def async_latest_timestamp(self) -> int:
        return await asyncio.to_thread(self.latest_timestamp)


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


def read_private_key_file(path: Path | str) -> str:
    """Read a hex private key from a regular file that only its owner can read.

    The file must not be readable or writable by group or others (mode 0600 or stricter).
    The key never appears on a command line, in a log line, or in an error message.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise FileNotFoundError(f"Private key file not found: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Private key file must be a regular file, not a link or directory: {path}")
    if info.st_mode & 0o077:
        raise PermissionError(
            f"Private key file {path} is readable by group or others; run: chmod 600 {path}"
        )
    key = path.read_text().strip()
    if not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", key):
        raise ValueError(f"Private key file {path} does not hold one 32-byte hex key")
    return key


def parse_public_key(key: list[int] | str | bytes) -> list[int]:
    """Parse 33-byte compressed secp256k1 public key from hex string, bytes, or int list."""
    if isinstance(key, str):
        raw = key.strip().lower()
        if raw.startswith("0x"):
            raw = raw[2:]
        b = bytes.fromhex(raw)
    elif isinstance(key, (bytes, bytearray, list)):
        b = bytes(key)
    else:
        raise TypeError(f"Unsupported public key type: {type(key)}")

    if len(b) != 33:
        raise ValueError(f"Expected 33-byte compressed public key, got {len(b)} bytes")
    return list(b)


def _resolve_authority_public_key(
    explicit_key: list[int] | str | None,
    key_name: str,
    manifest: dict,
    manifest_path: Path | None = None,
) -> list[int]:
    """Resolve a 33-byte authority public key from argument, public_keys.yaml, or manifest."""
    if explicit_key is not None:
        return parse_public_key(explicit_key)

    # 1. Check public_keys file referenced in manifest.public_files.public_keys
    pub_file_rel = manifest.get("public_files", {}).get("public_keys")
    candidates: list[Path] = []
    if pub_file_rel:
        p = Path(pub_file_rel)
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates.append(REPO_ROOT / p)
            if manifest_path:
                candidates.append(manifest_path.parent / p)
                candidates.append(manifest_path.parent / p.name)
    elif manifest_path:
        candidates.append(manifest_path.parent / "public_keys.yaml")
    else:
        candidates.append(REPO_ROOT / "showcases" / "ionet_arc" / "config" / "public_keys.yaml")

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            try:
                data = yaml.safe_load(candidate.read_text()) or {}
                if isinstance(data, dict) and key_name in data:
                    return parse_public_key(data[key_name])
            except Exception:
                pass

    # 2. Check inline public_keys in manifest (for backwards compatibility / inline test fixtures)
    if key_name in manifest.get("public_keys", {}):
        return parse_public_key(manifest["public_keys"][key_name])

    # 3. Check public_key_files in manifest
    if key_name in manifest.get("public_key_files", {}):
        pub_path = REPO_ROOT / manifest["public_key_files"][key_name]
        if pub_path.exists():
            return parse_public_key(pub_path.read_text().strip())

    raise FileNotFoundError(
        f"Authority public key '{key_name}' missing: not provided in arguments or public_keys.yaml"
    )


def compile_buyer_policy(
    template_path: Path | None = None,
    manifest_path: Path | None = None,
    chain_id: int | None = None,
    escrow_address: str | None = None,
    token_address: str | None = None,
    max_amount: int | None = None,
    valid_after_buffer: int = 300,
    validity_seconds: int = 900,
    registry_public_key: list[int] | str | None = None,
    acceptance_public_key: list[int] | str | None = None,
) -> tuple[dict, str]:
    """Compile buyer policy template into concrete Policy and compute its cryptographic policy_hash."""
    manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
    template_path = template_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml")

    manifest = load_manifest(manifest_path)
    template = yaml.safe_load(template_path.read_text()) if template_path.exists() else {}

    chain_id = chain_id or manifest.get("chain_id", DEFAULT_CHAIN_ID)
    escrow_address = escrow_address or get_manifest_parameter(manifest, "escrow_address", "escrow-address")
    token_address = token_address or get_manifest_parameter(manifest, "token_address", "token-address")
    if max_amount is None:
        raw_budget = get_manifest_parameter(manifest, "max_budget_usd", "max-budget-usd")
        if raw_budget is not None:
            max_amount = int(Decimal(str(raw_budget)) * Decimal(1_000_000))
        else:
            raw_amount = get_manifest_parameter(manifest, "max_amount", "max-amount")
            max_amount = int(raw_amount) if raw_amount is not None else 2_000_000
    validity_seconds = int(manifest.get("validity_seconds", validity_seconds))

    if not escrow_address or not token_address:
        raise ValueError("escrow_address and token_address must be specified")

    reg_pub = _resolve_authority_public_key(
        registry_public_key, "registry_key", manifest, manifest_path=manifest_path
    )
    acc_pub = _resolve_authority_public_key(
        acceptance_public_key, "buyer_acceptance_key", manifest, manifest_path=manifest_path
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

    return policy, hash_policy(policy)


def hash_policy(policy: dict) -> str:
    """Compute cryptographic policy_hash of a Policy dict using warrant-policy."""
    policy_bin = find_warrant_binary("warrant-policy")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(policy, f)
        temp_file = Path(f.name)

    try:
        res = subprocess.run([str(policy_bin), "hash", str(temp_file)], capture_output=True, text=True, check=True)
        return res.stdout.strip()
    finally:
        temp_file.unlink(missing_ok=True)


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


def prove_authorization(policy: dict, request: dict, evidence: dict, work_dir: Path | str) -> dict:
    """Produce a real Groth16 proof of the authorization and export it for the EVM verifier.

    Runs ``warrant-host prove``, ``wrap`` and ``export-evm`` in ``work_dir``. ``wrap`` needs an x86 host
    with Docker. Fake receipts are refused: the host binary rejects them, and ``RISC0_DEV_MODE`` is refused
    here before any work starts. Returns the exported proof: ``imageId``, ``journal``, ``journalDigest``
    and ``seal`` as 0x-prefixed hex strings. The caller must compare ``journal`` with the native journal.
    """
    if os.environ.get("RISC0_DEV_MODE"):
        raise RuntimeError("RISC0_DEV_MODE is set; fake receipts are prohibited for settlement")
    host_bin = find_warrant_binary("warrant-host")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    input_file = work / "input.json"
    receipt = work / "request.receipt"
    groth16 = work / "request-groth16.receipt"
    exported = work / "evm.json"
    for path in (receipt, groth16, exported):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite an existing proof artifact: {path}")
    input_file.write_text(json.dumps({"policy": policy, "request": request, "evidence": evidence}))

    def run(*args: str) -> None:
        res = subprocess.run([str(host_bin), *args], capture_output=True, text=True, check=False)
        if res.returncode != 0:
            tail = (res.stderr or res.stdout).strip()[-1500:]
            raise RuntimeError(f"warrant-host {args[0]} failed: {tail}")

    run("prove", str(input_file), str(receipt))
    run("wrap", str(receipt), str(groth16))
    run("export-evm", str(groth16), str(exported))
    proof = json.loads(exported.read_text())
    for key in ("imageId", "journal", "journalDigest", "seal"):
        value = proof.get(key)
        if not isinstance(value, str) or not value.startswith("0x"):
            raise RuntimeError(f"Exported proof is missing {key}")
    return proof


def compute_task_id(chain_id: int, escrow_address: str, customer_address: str, salt: str | bytes) -> str:
    """Derive deterministic TaskEscrow task_id matching keccak256(abi.encode(chain_id, escrow, customer, salt))."""
    encoded = encode(
        ["uint256", "address", "address", "bytes32"],
        [int(chain_id), to_checksum_address(escrow_address), to_checksum_address(customer_address), bytes32(salt)],
    )
    return "0x" + keccak(encoded).hex()


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
        self._task_id_cache: dict[tuple[str, str, bytes], str] = {}

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
        if not pk:
            for env_var in (
                "WARRANT_PRIVATE_KEY",
                "WARRANT_BUYER_KEY",
                "WARRANT_SELLER_KEY",
                "BUYER_PRIVATE_KEY",
                "SELLER_PRIVATE_KEY",
            ):
                candidate_pk = os.environ.get(env_var)
                if candidate_pk:
                    try:
                        acc = Account.from_key(candidate_pk)
                        if acc.address.lower() == from_addr.lower():
                            pk = candidate_pk
                            break
                    except Exception:
                        pass

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
            try:
                tx_hash = self.rpc.rpc("eth_sendTransaction", [tx_params])
            except Exception as e:
                err_str = str(e)
                if "-32601" in err_str or "not supported" in err_str or "unsupported" in err_str:
                    raise RuntimeError(
                        f"Failed to send transaction from {from_addr} on {self.rpc_url}: "
                        f"RPC node does not support eth_sendTransaction (-32601) and no private key "
                        f"was configured to sign the transaction locally. Configure a private key via "
                        f"manifest.yaml private_key_files, environment variable (WARRANT_PRIVATE_KEY, "
                        f"WARRANT_BUYER_KEY, or WARRANT_SELLER_KEY), or CLI flag (--private-key)."
                    ) from e
                raise

        receipt = self.wait_for_receipt(tx_hash)
        status = receipt.get("status")
        if status is not None and int(str(status), 16) != 1:
            raise RuntimeError(f"Transaction reverted: {receipt}")
        return receipt

    def wait_for_receipt(self, tx_hash: str, timeout: float = 120.0) -> dict:
        started = time.time()
        while time.time() - started < timeout:
            receipt = self.rpc.rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(1.0 if should_trust_env(self.rpc_url) else 0.05)
        raise TimeoutError(f"Transaction receipt timeout for {tx_hash}; read the chain before any retry")

    def task_id_for(self, customer: str, salt: str) -> str:
        """Call TaskEscrow.taskIdFor(customer, salt) with result caching and offline fallback."""
        cache_key = (
            to_checksum_address(self.escrow) if self.escrow else "",
            to_checksum_address(customer),
            bytes32(salt),
        )
        if cache_key in self._task_id_cache:
            return self._task_id_cache[cache_key]

        try:
            res = self.call_contract(
                self.escrow,
                "taskIdFor(address,bytes32)",
                ["address", "bytes32"],
                [cache_key[1], cache_key[2]],
                ["bytes32"],
            )
            tid = "0x" + res[0].hex()
        except Exception:
            if self.escrow:
                try:
                    chain_id = self.rpc.chain_id()
                except Exception:
                    chain_id = DEFAULT_CHAIN_ID
                tid = compute_task_id(chain_id, self.escrow, customer, salt)
            else:
                raise
        self._task_id_cache[cache_key] = tid
        return tid

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

    def image_id(self) -> str:
        """Read the guest image ID the escrow was deployed with (TaskEscrow.imageId)."""
        res = self.call_contract(self.escrow, "imageId()", [], [], ["bytes32"])
        return "0x" + res[0].hex()

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
        if int(self.rpc.chain_id()) in MAINNET_CHAIN_IDS:
            raise RuntimeError("Mock settlement is refused on a mainnet chain; use a real proof")
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

    async def async_task_id_for(self, customer: str, salt: str) -> str:
        return await asyncio.to_thread(self.task_id_for, customer, salt)

    async def async_get_task(self, task_id: str) -> dict:
        return await asyncio.to_thread(self.get_task, task_id)

    async def async_image_id(self) -> str:
        return await asyncio.to_thread(self.image_id)

    async def async_accept(self, task_id: str, sender: str | None = None) -> dict:
        return await asyncio.to_thread(self.accept, task_id, sender)

    async def async_settle_mock(
        self,
        journal_bytes: bytes,
        sender: str | None = None,
        seal_hex: str = MOCK_SEAL,
    ) -> dict:
        return await asyncio.to_thread(self.settle_mock, journal_bytes, sender, seal_hex)

    async def async_settle(
        self,
        seal: bytes | str,
        journal_bytes: bytes,
        sender: str | None = None,
    ) -> dict:
        return await asyncio.to_thread(self.settle, seal, journal_bytes, sender)

    async def async_offer(
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
        return await asyncio.to_thread(
            self.offer,
            salt,
            recipient,
            policy_hash,
            policy_version,
            amount,
            accept_by,
            settle_by,
            sender,
        )

    async def async_token_balance(self, account: str) -> int:
        return await asyncio.to_thread(self.token_balance, account)

    async def async_approve_token(
        self,
        spender: str,
        amount: int,
        sender: str | None = None,
    ) -> dict:
        return await asyncio.to_thread(self.approve_token, spender, amount, sender)

    async def async_mint_token(
        self,
        recipient: str,
        amount: int,
        sender: str | None = None,
    ) -> dict:
        return await asyncio.to_thread(self.mint_token, recipient, amount, sender)


def ensure_policy_hash(
    escrow_address: str,
    token_address: str,
    chain_id: int,
    max_amount: int,
    state_dir: Path | None = None,
    categories: list[int] | None = None,
    valid_after: int = 1000,
    valid_until: int = 2_000_000_000,
    manifest_path: Path | None = None,
    template_path: Path | None = None,
) -> str:
    """Instantiate and hash policy for this scope, failing loudly if configs or keys are missing."""
    manifest_path = manifest_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "manifest.yaml")
    template_path = template_path or (REPO_ROOT / "showcases" / "ionet_arc" / "config" / "buyer_policy_template.yaml")

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    if not template_path.exists():
        raise FileNotFoundError(f"Policy template file not found: {template_path}")

    _, pol_hash = compile_buyer_policy(
        template_path=template_path,
        manifest_path=manifest_path,
        chain_id=chain_id,
        escrow_address=escrow_address,
        token_address=token_address,
        max_amount=max_amount,
    )
    return pol_hash

