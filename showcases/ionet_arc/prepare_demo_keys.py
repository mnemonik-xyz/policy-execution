#!/usr/bin/env python3
"""Prepare authority keys, buyer policy template, and signed VendorCredential."""

import argparse
import contextlib
import datetime
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ARC_TESTNET_CHAIN_ID = 5042002
ARC_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
DEFAULT_ESCROW_ADDRESS = "0x9449C1fDd068cDe33D900b1ab15f9861dc39bC51"
DEFAULT_VERIFIER_ADDRESS = "0x0932F120DEe4cA83676B8224769eE52b7d6D00c6"
DEFAULT_SELLER_ADDRESS = "0x9F188809b2ba2e29653693C4069c212a95C1B8c4"
DEFAULT_CATEGORY = 42
DEFAULT_MAX_AMOUNT = 2_000_000  # 2.00 USDC (6 decimals)
DEFAULT_VALIDITY_SECONDS = 3600


def dump_yaml(data: any) -> str:
    return yaml.dump(data, sort_keys=False, default_flow_style=False)


def annotate_timestamps(text: str, valid_after: int, valid_until: int) -> str:
    after_str = datetime.datetime.fromtimestamp(valid_after, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    until_str = datetime.datetime.fromtimestamp(valid_until, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    text = re.sub(rf"(valid_after:\s*{valid_after})(?!\s*#)", rf"\1  # {after_str}", text)
    text = re.sub(rf"(valid_until:\s*{valid_until})(?!\s*#)", rf"\1  # {until_str}", text)
    return text


def annotate_amount(text: str, amount: int) -> str:
    comment = f"  # {amount / 1_000_000:.2f} USDC"
    text = re.sub(rf"(max_amount:\s*{amount})(?!\s*#)", rf"\1{comment}", text)
    text = re.sub(rf"(amount_at_most:\s*{amount})(?!\s*#)", rf"\1{comment}", text)
    return text


def to_hex(data: list[int] | bytes) -> str:
    return "0x" + bytes(data).hex()


def from_hex(hex_str: str) -> list[int]:
    raw = hex_str.strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    return list(bytes.fromhex(raw))


def find_binary(name: str) -> pathlib.Path:
    for candidate in [
        REPO_ROOT / "target" / "release" / name,
        REPO_ROOT / "target" / "debug" / name,
    ]:
        if candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"Could not find compiled binary '{name}'. Run 'cargo build --release -p warrant-host'."
    )


def ensure_key_file(path: pathlib.Path, force: bool = False) -> str:
    if path.exists() and not force:
        content = path.read_text().strip()
        if content:
            return content
    key_hex = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key_hex + "\n")
    with contextlib.suppress(Exception):
        os.chmod(path, 0o600)
    return key_hex


def get_public_key_hex(evidence_bin: pathlib.Path, key_path: pathlib.Path) -> str:
    res = subprocess.run(
        [str(evidence_bin), "public-key", str(key_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    raw_bytes = json.loads(res.stdout.strip())
    return to_hex(raw_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare keys, buyer policy template, and signed VendorCredential."
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=int(os.environ.get("ARC_CHAIN_ID", ARC_TESTNET_CHAIN_ID)),
        help=f"Target blockchain chain ID (default: {ARC_TESTNET_CHAIN_ID})",
    )
    parser.add_argument(
        "--escrow",
        default=os.environ.get("WARRANT_ESCROW", DEFAULT_ESCROW_ADDRESS),
        help=f"TaskEscrow contract address (default: {DEFAULT_ESCROW_ADDRESS})",
    )
    parser.add_argument(
        "--verifier",
        default=os.environ.get("WARRANT_VERIFIER", DEFAULT_VERIFIER_ADDRESS),
        help=f"RiscZeroGroth16Verifier address (default: {DEFAULT_VERIFIER_ADDRESS})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("WARRANT_TOKEN", ARC_USDC_ADDRESS),
        help=f"ERC-20 payment token address (default: {ARC_USDC_ADDRESS})",
    )
    parser.add_argument(
        "--seller-address",
        default=os.environ.get("WARRANT_SELLER_ADDRESS", DEFAULT_SELLER_ADDRESS),
        help=f"Seller recipient Ethereum address (default: {DEFAULT_SELLER_ADDRESS})",
    )
    parser.add_argument(
        "--category",
        type=int,
        default=int(os.environ.get("WARRANT_CATEGORY", DEFAULT_CATEGORY)),
        help=f"Certified vendor category ID (default: {DEFAULT_CATEGORY})",
    )
    parser.add_argument(
        "--max-amount",
        type=int,
        default=int(os.environ.get("WARRANT_MAX_AMOUNT", DEFAULT_MAX_AMOUNT)),
        help=f"Buyer policy max amount cap in atomic token units (default: {DEFAULT_MAX_AMOUNT})",
    )
    parser.add_argument(
        "--validity-seconds",
        type=int,
        default=int(os.environ.get("WARRANT_VALIDITY_SECONDS", DEFAULT_VALIDITY_SECONDS)),
        help=f"Default validity duration in seconds for requests (default: {DEFAULT_VALIDITY_SECONDS})",
    )
    parser.add_argument(
        "--config-dir",
        "--out-dir",
        type=pathlib.Path,
        dest="config_dir",
        default=REPO_ROOT / "showcases" / "ionet_arc" / "config",
        help="Public directory to store public policy template and credentials (default: showcases/ionet_arc/config)",
    )
    parser.add_argument(
        "--keys-dir",
        type=pathlib.Path,
        default=REPO_ROOT / "showcases" / "ionet_arc" / ".keys",
        help="Private directory to store secret authority keys (default: showcases/ionet_arc/.keys)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing key files if present",
    )
    args = parser.parse_args()

    config_dir = args.config_dir.resolve()
    config_dir.mkdir(parents=True, exist_ok=True)

    keys_dir = args.keys_dir.resolve()
    keys_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        os.chmod(keys_dir, 0o700)

    for old_key in config_dir.glob("*.key"):
        target = keys_dir / old_key.name
        if not target.exists():
            shutil.copy2(old_key, target)
        old_key.unlink()

    for stale_file in [
        config_dir / "buyer_policy.yaml",
        config_dir / "buyer_policy_parameters.yaml",
        *config_dir.glob("*.json"),
    ]:
        if stale_file.exists():
            stale_file.unlink()

    evidence_bin = find_binary("warrant-evidence")

    # 1. Authority private keys in .keys/
    reg_key_path = keys_dir / "registry.key"
    acc_key_path = keys_dir / "buyer_acceptance.key"

    ensure_key_file(reg_key_path, force=args.force)
    ensure_key_file(acc_key_path, force=args.force)

    # 2. Derive compressed public keys (33-byte SEC1)
    reg_pub_hex = get_public_key_hex(evidence_bin, reg_key_path)
    acc_pub_hex = get_public_key_hex(evidence_bin, acc_key_path)

    # 3. VendorCredential for seller (1-year validity window)
    now = int(time.time())
    cred_valid_after = now - 3600
    cred_valid_until = now + (365 * 86400)

    credential_yaml_obj = {
        "scope": {
            "chain_id": args.chain_id,
            "vault": args.escrow,
            "token": args.token,
        },
        "recipient": args.seller_address,
        "category": args.category,
        "valid_after": cred_valid_after,
        "valid_until": cred_valid_until,
    }

    credential_yaml_path = config_dir / "vendor_credential.yaml"
    signature_yaml_path = config_dir / "vendor_signature.yaml"

    cred_header = (
        "# Vendor credential attesting seller category 42 accreditation.\n"
        "# Re-verified in zkVM during settlement against registry_key.\n"
    )
    raw_cred_yaml = dump_yaml(credential_yaml_obj)
    annotated_cred = annotate_timestamps(raw_cred_yaml, cred_valid_after, cred_valid_until)
    credential_yaml_path.write_text(cred_header + annotated_cred)

    credential_rust_obj = {
        "scope": {
            "chain_id": args.chain_id,
            "vault": from_hex(args.escrow),
            "token": from_hex(args.token),
        },
        "recipient": from_hex(args.seller_address),
        "category": args.category,
        "valid_after": cred_valid_after,
        "valid_until": cred_valid_until,
    }

    # 4. Sign VendorCredential with registry.key
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = pathlib.Path(tmp_dir)
        temp_cred = tmp_path / "cred.json"
        temp_sig = tmp_path / "sig.json"
        temp_cred.write_text(json.dumps(credential_rust_obj))

        subprocess.run(
            [
                str(evidence_bin),
                "sign-vendor",
                str(reg_key_path),
                str(temp_cred),
                str(temp_sig),
            ],
            check=True,
        )
        sig_bytes = json.loads(temp_sig.read_text())
        sig_hex = to_hex(sig_bytes)
        sig_header = "# 64-byte ECDSA signature of vendor_credential.yaml signed by registry.key.\n"
        signature_yaml_path.write_text(sig_header + dump_yaml({"signature": sig_hex}))

    # 5. Upfront Buyer policy template (validity window stamped dynamically per request)
    policy_template_obj = {
        "version": 1,
        "scope": {
            "chain_id": args.chain_id,
            "vault": args.escrow,
            "token": args.token,
        },
        "registry_key": reg_pub_hex,
        "acceptance_key": acc_pub_hex,
        "rule": {
            "all": [
                "accepted",
                {"amount_at_most": args.max_amount},
                {"vendor_category_in": [args.category]},
            ]
        },
        "validity_duration_seconds": args.validity_seconds,
    }

    template_header = (
        "# Buyer policy template for TaskEscrow on Arc Testnet.\n"
        "# Concrete policy timestamps (valid_after/valid_until) are stamped per request.\n"
    )
    template_yaml_path = config_dir / "buyer_policy_template.yaml"
    raw_template_yaml = dump_yaml(policy_template_obj)
    template_yaml_path.write_text(template_header + annotate_amount(raw_template_yaml, args.max_amount))

    # 6. Showcase master manifest linking contracts, policy template, and public credentials
    manifest = {
        "chain_id": args.chain_id,
        "escrow_address": args.escrow,
        "verifier_address": args.verifier,
        "token_address": args.token,
        "seller_address": args.seller_address,
        "category": args.category,
        "max_amount": args.max_amount,
        "public_files": {
            "manifest": str((config_dir / "showcase_manifest.yaml").relative_to(REPO_ROOT)),
            "buyer_policy_template": str(template_yaml_path.relative_to(REPO_ROOT)),
            "vendor_credential": str(credential_yaml_path.relative_to(REPO_ROOT)),
            "vendor_signature": str(signature_yaml_path.relative_to(REPO_ROOT)),
        },
        "private_key_files": {
            "registry_key": str(reg_key_path.relative_to(REPO_ROOT)),
            "buyer_acceptance_key": str(acc_key_path.relative_to(REPO_ROOT)),
        },
    }

    manifest_header = (
        "# Showcase manifest linking deployed contracts, policy template, and credentials.\n"
    )
    manifest_yaml_path = config_dir / "showcase_manifest.yaml"
    raw_manifest_yaml = dump_yaml(manifest)
    manifest_yaml_path.write_text(manifest_header + annotate_amount(raw_manifest_yaml, args.max_amount))

    # 7. Print summary
    summary = {
        "warrant_demo_setup": {
            "status": "success",
            "chain_id": args.chain_id,
            "escrow_address": args.escrow,
            "verifier_address": args.verifier,
            "token_address": args.token,
            "seller_address": args.seller_address,
            "category": args.category,
            "max_amount": args.max_amount,
            "config_dir": str(config_dir.relative_to(REPO_ROOT)),
            "keys_dir": str(keys_dir.relative_to(REPO_ROOT)),
            "policy_mode": "dynamic_per_request",
            "policy_template": str(template_yaml_path.relative_to(REPO_ROOT)),
            "manifest_file": str(manifest_yaml_path.relative_to(REPO_ROOT)),
        }
    }
    print(dump_yaml(summary).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
