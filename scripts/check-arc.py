#!/usr/bin/env python3
"""Read-only Arc checks for the task-escrow deployment. Nothing here broadcasts or funds an account.

Two phases:

  probe   Before deployment. Confirms the chain ID, the BN254 precompiles, the USDC decimals, and a real
          proof, by simulating the ArcProbe constructor with eth_call: the verifier accepts the proof,
          rejects a changed journal, and the escrow constructors run.

  verify  After deployment and before any order is funded. Reads the escrow and verifier immutables,
          compares the deployed runtime code with the local build (only the immutable words may differ),
          and writes a deployment manifest: chain, addresses, code hashes, image ID, compiler, source
          commit and the broadcast transaction hashes.

Examples:
  python3 scripts/check-arc.py probe contracts/test/fixtures/risc0-3.0.5.json --network testnet
  python3 scripts/check-arc.py verify --network mainnet --escrow 0x... --verifier 0x... \\
      --image-id 0x... --out artifacts/mainnet/deployment-manifest.json
"""
import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
USDC = "0x3600000000000000000000000000000000000000"
NETWORKS = {
    "testnet": {"chain_id": 5042002, "rpc": "https://rpc.testnet.arc.io"},
    "mainnet": {"chain_id": 5042, "rpc": "https://rpc.mainnet.arc.io"},
}
WORD = 32


class CheckError(Exception):
    pass


def run(*command, cwd=CONTRACTS):
    try:
        res = subprocess.run([str(c) for c in command], cwd=cwd, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise CheckError(f"Command not found: {command[0]}") from exc
    if res.returncode:
        raise CheckError(f"{command[0]} {command[1] if len(command) > 1 else ''} failed:\n"
                         f"{(res.stderr + res.stdout).strip()[-1500:]}")
    return res.stdout.strip()


def rpc_call(rpc, method, *params):
    return json.loads(run("cast", "rpc", "--rpc-url", rpc, method, *[json.dumps(p) for p in params]))


def eth_call(rpc, to, data):
    return rpc_call(rpc, "eth_call", {"to": to, "data": data}, "latest")


def hexbytes(text):
    text = text.strip()
    if text.startswith("0x"):
        text = text[2:]
    return bytes.fromhex(text)


def word(value):
    """A 32-byte word from a hex string: addresses are right-aligned, bytes16 values left-aligned."""
    raw = hexbytes(value)
    if len(raw) == 20:
        return raw.rjust(WORD, b"\0")
    if len(raw) == 16:
        return raw.ljust(WORD, b"\0")
    if len(raw) == WORD:
        return raw
    raise CheckError(f"Cannot form a 32-byte word from {len(raw)} bytes")


def compare_runtime_code(deployed, local, immutables):
    """True when deployed and local runtime code differ only in whole 32-byte words that hold an immutable.

    ``immutables`` is the set of 32-byte words the chain reports through the public getters. The local
    build holds zero words at those positions. The metadata hash is part of both and must agree, so a
    different source or compiler fails here.
    """
    if len(deployed) != len(local):
        return False
    allowed = {bytes(w) for w in immutables}
    zero = b"\0" * WORD
    i = 0
    while i < len(deployed):
        if deployed[i] == local[i]:
            i += 1
            continue
        # An immutable word can start up to 31 bytes before the first differing byte: an address word
        # begins with twelve zero bytes that match the zero placeholder of the local build.
        for start in range(max(0, i - WORD + 1), i + 1):
            if local[start:start + WORD] == zero and deployed[start:start + WORD] in allowed:
                i = start + WORD
                break
        else:
            return False
    return True


def keccak(data):
    return run("cast", "keccak", "0x" + data.hex())


def chain_id(rpc):
    return int(run("cast", "chain-id", "--rpc-url", rpc))


def require_chain(rpc, network):
    expected = NETWORKS[network]["chain_id"]
    actual = chain_id(rpc)
    if actual != expected:
        raise CheckError(f"RPC reports chain {actual}; {network} is {expected}")
    return actual


def check_precompiles(rpc):
    """BN254 addition (0x06), multiplication (0x07) and pairing (0x08) must answer. The verifier needs all three."""
    g1 = "0x" + "00" * 31 + "01" + "00" * 31 + "02"
    double = eth_call(rpc, "0x" + "00" * 19 + "06", g1 + g1[2:])
    if len(hexbytes(double)) != 64:
        raise CheckError("BN254 addition precompile (0x06) did not return a point")
    scaled = eth_call(rpc, "0x" + "00" * 19 + "07", g1 + "00" * 31 + "02")
    if scaled.lower() != double.lower():
        raise CheckError("BN254 multiplication precompile (0x07) disagrees with addition")
    empty = eth_call(rpc, "0x" + "00" * 19 + "08", "0x")
    if int(empty, 16) != 1:
        raise CheckError("BN254 pairing precompile (0x08) rejected the empty pairing")


def check_usdc(rpc):
    decimals = int(eth_call(rpc, USDC, "0x313ce567"), 16)
    if decimals != 6:
        raise CheckError(f"USDC reports {decimals} decimals, expected 6")


def cmd_probe(args):
    rpc = args.rpc or NETWORKS[args.network]["rpc"]
    actual = require_chain(rpc, args.network)
    check_usdc(rpc)
    check_precompiles(rpc)
    proof = json.loads(pathlib.Path(args.proof).read_text())
    run("forge", "build")
    bytecode = run("forge", "inspect", "ArcProbe", "bytecode")
    encoded = run("cast", "abi-encode", "constructor(address,bytes,bytes32,bytes32)",
                  USDC, proof["seal"], proof["imageId"], proof["journalDigest"])
    result = rpc_call(rpc, "eth_call", {"data": bytecode + encoded[2:], "gas": hex(15_000_000)}, "latest")
    if result != "0x" + "00" * 31 + "01":
        raise CheckError(f"ArcProbe simulation did not return 1: {result}")
    print(f"Arc {args.network} (chain {actual}) read-only probe passed: USDC decimals, BN254 precompiles, "
          "real proof accepted, changed journal rejected, task and invoice escrow constructors run.")


def getter(rpc, address, signature):
    return eth_call(rpc, address, run("cast", "sig", signature))


def control_ids():
    text = (CONTRACTS / "vendor/risc0/contracts/src/groth16/ControlID.sol").read_text()
    found = dict(re.findall(r"bytes32 public constant (\w+) = hex\"([0-9a-fA-F]{64})\"", text))
    if {"CONTROL_ROOT", "BN254_CONTROL_ID"} - set(found):
        raise CheckError("ControlID.sol does not define CONTROL_ROOT and BN254_CONTROL_ID")
    root = hexbytes(found["CONTROL_ROOT"])
    return root[:16], root[16:], hexbytes(found["BN254_CONTROL_ID"])


def expected_image_id(args):
    if args.image_id:
        return hexbytes(args.image_id)
    host = ROOT / "target/release/warrant-host"
    if not host.is_file():
        raise CheckError("Pass --image-id, or build warrant-host with RISC0_USE_DOCKER=1 first")
    return hexbytes(run(host, "image-id", cwd=ROOT))


def read_immutables(rpc, escrow, verifier):
    """The public getters of both contracts. Every value is compared with what the local build expects."""
    token = hexbytes(getter(rpc, escrow, "token()"))[-20:]
    linked = hexbytes(getter(rpc, escrow, "verifier()"))[-20:]
    image = hexbytes(getter(rpc, escrow, "imageId()"))
    root0 = hexbytes(getter(rpc, verifier, "CONTROL_ROOT_0()"))[:16]
    root1 = hexbytes(getter(rpc, verifier, "CONTROL_ROOT_1()"))[:16]
    bn254 = hexbytes(getter(rpc, verifier, "BN254_CONTROL_ID()"))
    return {"token": token, "verifier": linked, "image_id": image, "control_root_0": root0,
            "control_root_1": root1, "bn254_control_id": bn254}


def broadcast_transactions(actual_chain):
    latest = CONTRACTS / f"broadcast/Deploy.s.sol/{actual_chain}/run-latest.json"
    if not latest.is_file():
        return None
    data = json.loads(latest.read_text())
    return [{"contract": t.get("contractName"), "address": t.get("contractAddress"), "hash": t.get("hash")}
            for t in data.get("transactions", []) if t.get("transactionType") == "CREATE"]


def cmd_verify(args):
    rpc = args.rpc or NETWORKS[args.network]["rpc"]
    actual = require_chain(rpc, args.network)
    escrow, verifier = args.escrow, args.verifier
    for name, address in (("escrow", escrow), ("verifier", verifier)):
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
            raise CheckError(f"--{name} must be an address")
    image = expected_image_id(args)
    root0, root1, bn254 = control_ids()
    found = read_immutables(rpc, escrow, verifier)
    expected = {"token": hexbytes(USDC), "verifier": hexbytes(verifier), "image_id": image,
                "control_root_0": root0, "control_root_1": root1, "bn254_control_id": bn254}
    for key, value in expected.items():
        if found[key] != value:
            raise CheckError(f"Deployed {key} is 0x{found[key].hex()}, expected 0x{value.hex()}")

    run("forge", "build")
    deployed_escrow = hexbytes(run("cast", "code", escrow, "--rpc-url", rpc))
    deployed_verifier = hexbytes(run("cast", "code", verifier, "--rpc-url", rpc))
    local_escrow = hexbytes(run("forge", "inspect", "TaskEscrow", "deployedBytecode"))
    local_verifier = hexbytes(run("forge", "inspect", "RiscZeroGroth16Verifier", "deployedBytecode"))
    escrow_words = [word("0x" + found["token"].hex()), word("0x" + found["verifier"].hex()), found["image_id"]]
    verifier_words = [word("0x" + root0.hex()), word("0x" + root1.hex()), bn254]
    if not compare_runtime_code(deployed_escrow, local_escrow, escrow_words):
        raise CheckError("Deployed TaskEscrow code differs from the local build beyond its immutables")
    if not compare_runtime_code(deployed_verifier, local_verifier, verifier_words):
        raise CheckError("Deployed RiscZeroGroth16Verifier code differs from the local build beyond its immutables")

    foundry = (CONTRACTS / "foundry.toml").read_text()
    manifest = {
        "network": args.network,
        "chain_id": actual,
        "checked_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "source_commit": run("git", "rev-parse", "HEAD", cwd=ROOT),
        "source_dirty": bool(run("git", "status", "--porcelain", cwd=ROOT)),
        "compiler": {
            "solc": re.search(r'solc_version\s*=\s*"([^"]+)"', foundry).group(1),
            "evm_version": re.search(r'evm_version\s*=\s*"([^"]+)"', foundry).group(1),
            "optimizer_runs": int(re.search(r"optimizer_runs\s*=\s*(\d+)", foundry).group(1)),
        },
        "token": USDC,
        "escrow": {"address": escrow, "code_hash": keccak(deployed_escrow), "image_id": "0x" + image.hex()},
        "verifier": {
            "address": verifier,
            "code_hash": keccak(deployed_verifier),
            "control_root": "0x" + (root0 + root1).hex(),
            "bn254_control_id": "0x" + bn254.hex(),
        },
        "broadcast": broadcast_transactions(actual),
        "orders_funded": 0,
        "note": "Read-only verification. The deployed code matches the local build apart from immutables.",
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "x") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    print(json.dumps(manifest, indent=2))
    print(f"Deployment manifest written to {out}")


def parser():
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="pre-deployment read-only probe")
    probe.add_argument("proof", help="exported EVM proof JSON (seal, imageId, journalDigest)")
    verify = sub.add_parser("verify", help="post-deployment code check and deployment manifest")
    verify.add_argument("--escrow", required=True)
    verify.add_argument("--verifier", required=True)
    verify.add_argument("--image-id", help="expected guest image ID; default: target/release/warrant-host image-id")
    verify.add_argument("--out", default="artifacts/deployment-manifest.json", help="never overwritten")
    for p, handler in ((probe, cmd_probe), (verify, cmd_verify)):
        p.add_argument("--network", choices=sorted(NETWORKS), default="testnet")
        p.add_argument("--rpc", help="override the RPC URL of the selected network")
        p.set_defaults(handler=handler)
    return top


def main():
    args = parser().parse_args()
    try:
        args.handler(args)
    except (CheckError, FileExistsError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"check-arc: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
