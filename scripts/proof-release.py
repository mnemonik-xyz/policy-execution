#!/usr/bin/env python3
"""Build and check an evidence-bound strict invoice release candidate.

This manifest records specific proof scopes; it never certifies the entire
evidence adapter or a production deployment. No transaction is broadcast here.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "target/release/warrant-host"
SCHEMA = "warrant/proof-invoice-release/v1"
PROPERTIES = {
    "check_ceilingAcrossPayments", "check_closePreventsSettlement", "check_domainBinding",
    "check_exactPaymentAndReplay", "check_failedTransferIsAtomic", "check_noBypass",
    "check_proofRequired", "check_registryCannotBePoisoned",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def output(*args, env=None):
    return subprocess.check_output(args, cwd=ROOT, env=env, text=True).strip()


def sources():
    # Include uncommitted additions; omit generated ignored artifacts. Locks,
    # vendored verifier sources, specs and runners are all included.
    names = output("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    return {n: digest(ROOT / n) for n in sorted(set(names))
            if n and (ROOT / n).is_file()}


def validate_symbolic(report):
    rows = report.get("test_results", {}).get("test/ProofInvoiceEscrow.t.sol:ProofInvoiceEscrowTest", [])
    names = {r["name"].split("(")[0] for r in rows}
    if report.get("exitcode") != 0 or names != PROPERTIES or len(rows) != len(PROPERTIES):
        raise ValueError("Missing or failed symbolic properties")
    if any(r["exitcode"] != 0 or r["num_bounded_loops"] != 0 or r["num_models"] != 0 for r in rows):
        raise ValueError("Counterexample, bounded loop or incomplete symbolic check")


def validate_identity(info):
    image = info.get("imageId", "")
    if (info.get("guest") != "warrant-invoice-guest"
            or info.get("journalSchema") != "warrant/invoice-journal/v1"
            or info.get("journalBytes") != 480
            or len(image) != 66 or not image.startswith("0x") or int(image, 16) == 0):
        raise ValueError("Wrong guest, schema or zero image ID")


def validate_settlement(result, info):
    if (result.get("imageId") != info["imageId"]
            or result.get("tamperedJournalWordsRejected") != 15
            or not all(result.get(k) is True for k in (
                "proofOnly", "realProof", "replayRejected", "wrongImageRejected",
                "journalTamperingRejected", "deploymentCodeMatched"))):
        raise ValueError("Real settlement did not validate the release guest and deployed code")


def prepare(args):
    for key in ("RISC0_DEV_MODE", "RISC0_SKIP_BUILD", "WARRANT_DEMO_IMAGE_ID", "CARGO_TARGET_DIR"):
        if os.environ.get(key):
            raise ValueError(f"Unset {key}: candidate requires a real build and real proof")
    folder = (ROOT / "artifacts" / f"proof-release-{time.time_ns()}").resolve()
    folder.mkdir(parents=True)
    baseline = sources()
    env = dict(os.environ, RISC0_USE_DOCKER="1", RISC0_BUILD_LOCKED="1",
               DOCKER_DEFAULT_PLATFORM="linux/amd64", RAYON_NUM_THREADS="4")

    def run(name, command, extra=None):
        print(f"{name}: {folder / (name + '.log')}", flush=True)
        with (folder / (name + ".log")).open("w") as log:
            subprocess.run(command, cwd=ROOT, env=dict(env, **(extra or {})),
                           stdout=log, stderr=subprocess.STDOUT, check=True)

    run("release-tests", [sys.executable, "scripts/test_proof_release.py"])
    run("verus", [sys.executable, "verified/verify.py", "--verus", args.verus, "--mutations"])
    run("native-tests", ["cargo", "test", "--locked", "-p", "warrant-policy"])
    run("dependencies", ["npm", "ci", "--prefix", "contracts", "--ignore-scripts"])
    run("contract-tests", ["forge", "test", "--root", "contracts"])
    run("symbolic", [args.formal_python, "scripts/run-halmos.py", "--root", "contracts",
                     "--forge-build-out", "out-formal", "--contract", "ProofInvoiceEscrowTest",
                     "--solver", "z3", "--solver-timeout-assertion", "30000",
                     "--json-output", str(folder / "symbolic.json")], {"FOUNDRY_PROFILE": "formal"})
    validate_symbolic(json.loads((folder / "symbolic.json").read_text()))
    run("build", ["cargo", "build", "--locked", "--release", "-p", "warrant-host"])
    run("guest-tests", ["cargo", "test", "--locked", "--release", "-p", "warrant-host",
                        "--test", "execution", "--test", "tooling", "--", "--test-threads=1"])
    info = json.loads(output(str(HOST), "invoice-build-info", env=env))
    validate_identity(info)
    run("real-settlement", [sys.executable, "scripts/invoice-demo.py", "--proof-only",
                            "--output", str(folder / "settlement")])
    result = json.loads((folder / "settlement/result.json").read_text())
    validate_settlement(result, info)
    if sources() != baseline:
        raise ValueError("Source changed during verification/build; rerun from a stable checkout")
    manifest = dict(schema=SCHEMA, sourceRevision=output("git", "rev-parse", "HEAD"),
        sources=baseline, guest=info, hostSha256=digest(HOST),
        rust=output("rustc", "--version"), forge=output("forge", "--version"),
        buildEnvironment={k: env[k] for k in ("RISC0_USE_DOCKER", "RISC0_BUILD_LOCKED", "DOCKER_DEFAULT_PLATFORM")},
        endToEndFormallyVerified=False,
        verifiedScopes=["policy semantics", "payment field construction", "validity intersection",
                        "listed symbolic settlement properties under verifier/token models"],
        remainingGaps=["evidence authentication and facts projection proof", "parser correctness proof",
                       "journal encoding refinement proof", "unbounded settlement induction",
                       "independent review", "public deployment validation"],
        artifacts={str(p.relative_to(folder)): digest(p) for p in sorted(folder.rglob("*"))
                   if p.is_file() and p.suffix != ".key"},
        contractArtifacts={name: digest(ROOT / "contracts/out" / name) for name in (
            "ProofInvoiceEscrow.sol/ProofInvoiceEscrow.json", "ProofInvoiceFactory.sol/ProofInvoiceFactory.json")})
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Candidate complete (not an end-to-end formal certification): {manifest_path}")


def check(args):
    path = args.manifest.resolve()
    manifest = json.loads(path.read_text())
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unknown manifest schema")
    if manifest["sources"] != sources() or manifest["hostSha256"] != digest(HOST):
        raise ValueError("Source or host differs from the verified candidate")
    info = json.loads(output(str(HOST), "invoice-build-info"))
    validate_identity(info)
    if info != manifest["guest"]:
        raise ValueError("Embedded guest differs from the candidate")
    for name, expected in manifest["artifacts"].items():
        item = (path.parent / name).resolve()
        if not item.is_relative_to(path.parent) or digest(item) != expected:
            raise ValueError(f"Artifact changed: {name}")
    for name, expected in manifest["contractArtifacts"].items():
        item = (ROOT / "contracts/out" / name).resolve()
        if not item.is_relative_to(ROOT / "contracts/out") or digest(item) != expected:
            raise ValueError(f"Contract build changed: {name}")
    validate_symbolic(json.loads((path.parent / "symbolic.json").read_text()))
    validate_settlement(json.loads((path.parent / "settlement/result.json").read_text()), info)
    print("Candidate source, guest, proofs and contract artifacts match.")
    print("WARRANT_PROOF_ONLY=true")
    print("WARRANT_IMAGE_ID=" + info["imageId"])
    print("For a new guest in an existing replay domain, use the existing ProofInvoiceFactory.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--verus", required=True)
    p.add_argument("--formal-python", required=True, help="Python with halmos==0.3.3 installed")
    p = subs.add_parser("check")
    p.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        (prepare if args.command == "prepare" else check)(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release gate failed: {error}\n")


if __name__ == "__main__":
    main()
