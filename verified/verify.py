#!/usr/bin/env python3
"""Verify the runtime evaluator; optionally ensure representative bugs fail proof."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

VERSION = "0.2026.09.20.aef82ed"
SOURCE = Path(__file__).resolve().parent / "src" / "lib.rs"


def verify(verus, path):
    return subprocess.run(
        [verus, "--crate-type", "lib", "--edition", "2021", "--no-cheating",
         "--num-threads", "2", str(path)],
        capture_output=True, text=True, timeout=120,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verus", default=os.environ.get("VERUS", "verus"))
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    verus = shutil.which(args.verus)
    if not verus:
        parser.error("Verus not found; supply --verus /path/to/verus (see README.md)")
    version = subprocess.run([verus, "--version"], capture_output=True, text=True, check=True)
    if not re.search(r"Version: " + re.escape(VERSION) + r"\s", version.stdout):
        parser.error(f"expected pinned Verus {VERSION}; got {version.stdout.strip()}")
    result = verify(verus, SOURCE)
    print(result.stdout + result.stderr, end="", flush=True)
    if result.returncode or not re.search(r"[1-9][0-9]* verified, 0 errors", result.stdout):
        raise SystemExit("Evaluator verification failed")
    print("Verified source SHA-256:", hashlib.sha256(SOURCE.read_bytes()).hexdigest(), flush=True)
    if not args.mutations:
        return

    # Change only executable logic; the specification and proof obligations stay fixed.
    source = SOURCE.read_text()
    prefix, executable = source.split("pub fn evaluate(", 1)
    mutations = {
        "reversed amount limit": ("facts.amount <= *cap", "facts.amount >= *cap"),
        "exclusive amount boundary": ("facts.amount <= *cap", "facts.amount < *cap"),
        "acceptance bypass": ("Rule::Accepted => facts.accepted", "Rule::Accepted => true"),
        "conjunction bypass": ("if !evaluate(&children[i], facts)", "if evaluate(&children[i], facts)"),
        "disjunction inversion": ("if evaluate(&children[i], facts)", "if !evaluate(&children[i], facts)"),
        "category bypass": ("category_contains(categories, facts.category)", "true"),
        "recipient bypass": ("bytes_equal(&facts.recipient, recipient)", "true"),
        "deliverable bypass": ("bytes_equal(&facts.deliverable, hash)", "true"),
    }
    with tempfile.TemporaryDirectory(prefix="warrant-verus-mutations-") as directory:
        for name, (before, after) in mutations.items():
            if executable.count(before) != 1:
                raise SystemExit(f"Mutation needs updating: {name}")
            path = Path(directory) / "mutant.rs"
            path.write_text(prefix + "pub fn evaluate(" + executable.replace(before, after, 1))
            result = verify(verus, path)
            output = result.stdout + result.stderr
            # A compiler/setup failure is not evidence that a proof caught the bug.
            if result.returncode == 0 or "postcondition not satisfied" not in output:
                print(output)
                raise SystemExit(f"Mutation did not produce the expected proof failure: {name}")
            print(f"Rejected mutation: {name}", flush=True)
    print(f"All {len(mutations)} executable mutations rejected.")


if __name__ == "__main__":
    main()
