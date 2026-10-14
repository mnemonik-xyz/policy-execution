#!/usr/bin/env python3
"""Read-only probe of the solver path on Arc mainnet (chain 5042). Canary plan item 4.

Two phases. Both only read the chain: cast chain-id, cast call, cast code and cast rpc eth_call.
Nothing is broadcast, no key is read and no account is funded.

  before PROOF.json --out report.json
      Checks the chain ID, the BN254 precompiles at 0x06, 0x07 and 0x08, the USDC decimals, and simulates
      the creation of contracts/test/ArcSolverProbe.t.sol with eth_call. The probe verifies the real
      Groth16 seal from PROOF.json (the evm.json written by `warrant-host export-evm`: seal, imageId,
      journalDigest), rejects a changed journal digest and constructs SolverBountyEscrow.

  after MANIFEST.json --out report.json
      Reads the code of the deployed escrow and verifier, hashes it with keccak256 and compares the hashes
      with the manifest. Masks the immutable slots and compares the code with the local build artifacts in
      contracts/out. Checks token(), verifier(), imageId() and totalReserved() == 0 on the escrow.
      Manifest keys used (schema: solver-bounty/canary/manifest.schema.json; missing keys are reported):
      escrow, verifier, image_id, code_hashes {escrow, verifier}, token (default Arc USDC when absent),
      chain_id (default 5042 when absent) and transactions, which must not yet hold an order step.

The RPC URL comes from the WARRANT_CANARY_RPC variable. It is never put in arguments, reports or errors.
Any other chain than 5042 is refused unless --allow-chain N names it for a local rehearsal.
The report is created exclusively (open with "x") and is never overwritten. Exit status 1 on any problem.
"""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
MAINNET_ID = 5042
ARC_USDC = "0x3600000000000000000000000000000000000000"
RPC_ENV = "WARRANT_CANARY_RPC"
RISC0_DEV_MODE = "RISC0_DEV_MODE"
WORD_ONE = "0x" + "00" * 31 + "01"
EMPTY_CODE = "0x"
PROBE_GAS = 15_000_000
ORDER_STEPS = ("approve", "offer", "accept", "settle")
# BN254 generator G = (1, 2) and 2G, from the curve equation y^2 = x^3 + 3 over the base field (EIP-196).
G = "0x" + "00" * 31 + "01" + "00" * 31 + "02"
TWO_G = ("0x30644e72e131a029b85045b68181585d97816a916871ca8d3c208c16d87cfd3"
         "15ed738c0e0a7c92e7845f96b2ae9c0a68a6a449e3538fc7ff3ebf7a5a18a2c4")
# Address, calldata and expected eth_call result. ecPairing with empty input is defined as true (EIP-197).
PRECOMPILES = (
    ("ecadd", "0x" + "00" * 19 + "06", G + G[2:], TWO_G),
    ("ecmul", "0x" + "00" * 19 + "07", G + "00" * 31 + "02", TWO_G),
    ("ecpairing", "0x" + "00" * 19 + "08", "0x", WORD_ONE),
)


class ProbeError(Exception):
    pass


def rpc_url():
    url = os.environ.get(RPC_ENV, "")
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise ProbeError(f"Set {RPC_ENV} to an https RPC URL, or a loopback URL for a local rehearsal")
    return url


def redact(text):
    return text.replace(os.environ.get(RPC_ENV) or "\0", "<rpc>")


def run(*args, cwd=ROOT):
    """Run one command. Read-only commands only: this module never calls cast send or forge script."""
    env = dict(os.environ, ETH_RPC_URL=rpc_url()) if args[0] == "cast" else dict(os.environ)
    try:
        p = subprocess.run([str(a) for a in args], cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise ProbeError(f"Command not found: {args[0]}. Install Foundry first.") from exc
    if p.returncode:
        raise ProbeError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed:\n{redact(p.stderr + p.stdout)[-1500:]}")
    return redact(p.stdout.strip())


def call(address, signature, *values):
    return run("cast", "call", address, signature, *values)


def eth_call(request):
    return json.loads(run("cast", "rpc", "eth_call", json.dumps(request), "latest"))


def keccak(hex_data):
    return run("cast", "keccak", hex_data).lower()


def write_new(path, data):
    with open(path, "x") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")


def expected_chain(args):
    return args.allow_chain if args.allow_chain else MAINNET_ID


def check_chain(args, report, problems):
    actual = int(run("cast", "chain-id"))
    report["chain_id"] = actual
    if actual != expected_chain(args):
        problems.append(f"RPC reports chain {actual}; this probe is for chain {expected_chain(args)}")
    if RISC0_DEV_MODE in os.environ:
        problems.append(f"{RISC0_DEV_MODE} is set; fake receipts are prohibited")


def check_precompiles(report, problems):
    report["precompiles"] = {}
    for name, address, data, expected in PRECOMPILES:
        try:
            result = eth_call({"to": address, "data": data})
        except ProbeError as exc:
            result = None
            problems.append(f"{name} precompile call failed: {exc}")
        ok = isinstance(result, str) and result.lower() == expected
        report["precompiles"][name] = {"address": address, "ok": ok, "result": result}
        if result is not None and not ok:
            problems.append(f"{name} precompile at {address} returned an unexpected value")


def check_usdc(token, report, problems):
    decimals = int(call(token, "decimals()(uint8)").split()[0])
    report["usdc"] = {"address": token, "decimals": decimals}
    if decimals != 6:
        problems.append(f"token {token} reports {decimals} decimals, expected 6")


def load_proof(path):
    proof = json.loads(pathlib.Path(path).read_text())
    for key in ("seal", "imageId"):
        if not isinstance(proof.get(key), str) or not proof[key].startswith("0x"):
            raise ProbeError(f"Proof file lacks a hex {key!r} field")
    digest = proof.get("journalDigest")
    if not digest:
        if not isinstance(proof.get("journal"), str):
            raise ProbeError("Proof file lacks journalDigest and journal")
        digest = "0x" + hashlib.sha256(bytes.fromhex(proof["journal"][2:])).hexdigest()
    return proof["seal"], proof["imageId"], digest


def simulate_probe(args, token, report, problems):
    seal, image_id, digest = load_proof(args.proof)
    report["proof"] = {"file": str(args.proof), "image_id": image_id, "journal_digest": digest}
    if args.image_id and args.image_id.lower() != image_id.lower():
        problems.append("the proof image ID differs from --image-id")
    run("forge", "build", cwd=CONTRACTS)
    bytecode = run("forge", "inspect", "ArcSolverProbe", "bytecode", cwd=CONTRACTS)
    encoded = run("cast", "abi-encode", "constructor(address,bytes,bytes32,bytes32,uint256)",
                  token, seal, image_id, digest, str(args.allow_chain or 0))
    try:
        result = eth_call({"data": bytecode + encoded[2:], "gas": hex(PROBE_GAS)})
    except ProbeError as exc:
        report["probe"] = {"ok": False, "error": str(exc)}
        problems.append(f"probe simulation reverted: {exc}")
        return
    ok = isinstance(result, str) and result.lower() == WORD_ONE
    report["probe"] = {"ok": ok, "result": result}
    if not ok:
        problems.append("probe simulation returned an unexpected value")


def cmd_before(args):
    report, problems = {"phase": "before", "read_only": True}, []
    check_chain(args, report, problems)
    if report["chain_id"] == expected_chain(args):
        check_precompiles(report, problems)
        check_usdc(args.token, report, problems)
        simulate_probe(args, args.token, report, problems)
    return finish(args, report, problems)


def load_artifact(name):
    path = CONTRACTS / "out" / f"{name}.sol" / f"{name}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProbeError(f"Cannot read the build artifact for {name}; run forge build first") from exc


def masked(code, references):
    """Zero every immutable slot so that chain code and build output compare equal."""
    data = bytearray(bytes.fromhex(code[2:]))
    for slots in references.values():
        for slot in slots:
            start, length = slot["start"], slot["length"]
            if start + length > len(data):
                return None
            data[start:start + length] = bytes(length)
    return bytes(data)


def compare_with_build(name, chain_code, problems):
    deployed = load_artifact(name)["deployedBytecode"]
    local = deployed["object"]
    references = deployed.get("immutableReferences") or {}
    chain, build = masked(chain_code, references), masked(local, references)
    if len(local) != len(chain_code) or chain is None or build is None or chain != build:
        problems.append(f"{name} code on chain differs from the local build outside the immutable slots")
        return False
    return True


def manifest_value(manifest, *keys):
    value = manifest
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def cmd_after(args):
    report, problems = {"phase": "after", "read_only": True}, []
    manifest = json.loads(pathlib.Path(args.manifest).read_text())
    chain_id = manifest_value(manifest, "chain_id")
    if chain_id is not None and int(chain_id) != expected_chain(args):
        problems.append(f"manifest names chain {chain_id}; this probe is for chain {expected_chain(args)}")
    check_chain(args, report, problems)
    if problems:
        return finish(args, report, problems)
    token = manifest_value(manifest, "token") or args.token
    addresses = {"escrow": manifest_value(manifest, "escrow"), "verifier": manifest_value(manifest, "verifier")}
    image_id = manifest_value(manifest, "image_id")
    for key, value in list(addresses.items()) + [("image_id", image_id)]:
        if not isinstance(value, str) or not value.startswith("0x"):
            problems.append(f"manifest lacks a hex {key!r} field")
    for step in ORDER_STEPS:
        if manifest_value(manifest, "transactions", step):
            problems.append(f"manifest already records a {step} transaction; the probe runs before any order")
    if problems:
        return finish(args, report, problems)
    report["manifest"] = {"escrow": addresses["escrow"], "verifier": addresses["verifier"], "image_id": image_id,
                          "token": token}
    run("forge", "build", cwd=CONTRACTS)
    report["code"] = {}
    for name, contract in (("escrow", "SolverBountyEscrow"), ("verifier", "RiscZeroGroth16Verifier")):
        code = run("cast", "code", addresses[name]).lower()
        entry = report["code"][name] = {"address": addresses[name], "bytes": max(len(code) - 2, 0) // 2}
        if code == EMPTY_CODE:
            problems.append(f"no code at the {name} address")
            continue
        entry["keccak"] = keccak(code)
        expected = manifest_value(manifest, "code_hashes", name)
        entry["manifest_keccak"] = expected
        if not isinstance(expected, str) or expected.lower() != entry["keccak"]:
            problems.append(f"{name} code hash on chain differs from the manifest")
        entry["matches_build"] = compare_with_build(contract, code, problems)
    if any(entry.get("bytes") == 0 for entry in report["code"].values()):
        return finish(args, report, problems)
    escrow = addresses["escrow"]
    getters = {"token": call(escrow, "token()(address)"), "verifier": call(escrow, "verifier()(address)"),
               "imageId": call(escrow, "imageId()(bytes32)")}
    reserved = int(call(escrow, "totalReserved()(uint256)").split()[0])
    report["escrow"] = dict(getters, totalReserved=reserved)
    for name, want in (("token", token), ("verifier", addresses["verifier"]), ("imageId", image_id)):
        if getters[name].lower() != want.lower():
            problems.append(f"escrow.{name}() differs from the manifest")
    if reserved != 0:
        problems.append("an order is already funded (totalReserved != 0); the probe refuses to proceed")
    return finish(args, report, problems)


def finish(args, report, problems):
    report["problems"] = problems
    report["verdict"] = "ok" if not problems else "failed"
    write_new(args.out, report)
    print(f"{report['phase']}: {report['verdict']} ({len(problems)} problems), report {args.out}")
    for problem in problems:
        print(" -", problem)
    return 1 if problems else 0


def parser():
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", required=True)

    def add(name, handler, positional, help_text):
        p = sub.add_parser(name)
        p.add_argument(positional, help=help_text)
        p.add_argument("--out", required=True, help="Report path. The file must not exist yet.")
        p.add_argument("--token", default=ARC_USDC, help="ERC-20 address (default: Arc USDC)")
        p.add_argument("--allow-chain", type=int, default=0, metavar="N",
                       help="Accept chain N instead of 5042 for a local rehearsal")
        p.set_defaults(handler=handler)
        return p

    before = add("before", cmd_before, "proof", "evm.json from `warrant-host export-evm`")
    before.add_argument("--image-id", help="Expected guest image ID (hex), as printed by warrant-host solver-image-id")
    add("after", cmd_after, "manifest", "Deployment manifest JSON")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except FileExistsError as exc:
        print(f"Report file already exists, not overwritten: {exc.filename}", file=sys.stderr)
        return 1
    except (ProbeError, OSError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
