#!/usr/bin/env python3
"""Verify the swap evaluator and timeout arithmetic; optionally ensure representative bugs fail proof."""
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
MARKER = "// Executable evaluation"

# Each mutation changes executable code only; specifications and proofs stay fixed.
MUTATIONS = {
    "unknown conjunct ignored": ("None => unknown = true,\n                    Some(true) => {},",
                                 "None => {},\n                    Some(true) => {},"),
    "unknown disjunct ignored": ("None => unknown = true,\n                    Some(false) => {},",
                                 "None => {},\n                    Some(false) => {},"),
    "ask becomes allow": ("None => Decision::Ask,", "None => Decision::Allow,"),
    "deny becomes ask": ("Some(false) => Decision::Deny,", "Some(false) => Decision::Ask,"),
    "second chain ignored": ("Some(id_in_exec(set, &f3.give_chain) && id_in_exec(set, &f3.take_chain))",
                             "Some(id_in_exec(set, &f3.give_chain))"),
    "asset bypass": ("Some(id_in_exec(set, &f3.give_asset) && id_in_exec(set, &f3.take_asset))", "Some(true)"),
    "counterparty bypass": ("Some(id) => Some(id_in_exec(set, id)),", "Some(id) => Some(true),"),
    "unknown counterparty allowed": ("Some(id) => Some(id_in_exec(set, id)),\n            None => None,",
                                     "Some(id) => Some(id_in_exec(set, id)),\n            None => Some(true),"),
    "listed counterparty allowed": ("Some(bytes_equal(&c.list_hash, hash) && !c.listed)",
                                    "Some(bytes_equal(&c.list_hash, hash))"),
    "inclusive cap made exclusive": ("Some(v) => Some(v <= cap),", "Some(v) => Some(v < cap),"),
    "unknown quantity allowed": ("Some(v) => Some(v <= cap),\n        None => None,",
                                 "Some(v) => Some(v <= cap),\n        None => Some(true),"),
    "floor reversed": ("Some(v) => Some(v >= floor),", "Some(v) => Some(v <= floor),"),
    "period spend ignored": ("(s as u128) + (n as u128) > cap as u128", "(n as u128) > cap as u128"),
    "unknown period spend ignored": ("(None, _) => {\n                    unknown = true;\n                },",
                                     "(None, _) => {},"),
    "untracked period allowed": ("if !found {\n        Some(false)", "if !found {\n        Some(true)"),
    "finality depth off by one": ("Some(x) => x >= d,", "Some(x) => x + 1 >= d,"),
    "unknown finality allowed": ("} else if fin_false && shallow {\n                    Some(false)\n                } else {\n                    None",
                                 "} else if fin_false && shallow {\n                    Some(false)\n                } else {\n                    Some(true)"),
    "pair bypass": ("Some(pair_in_exec(pairs, &f3.give_asset, &f3.take_asset))", "Some(true)"),
    "evidence reversed": ("Some(e) => Some(e >= *m),", "Some(e) => Some(e <= *m),"),
    "risk flags bypass": ("Some(r) => Some(r & !*allowed == 0),", "Some(r) => Some(true),"),
    "pending ignores lead": ("(tt as u128) > (now.now_real as u128) + (b.max_lead_secs as u128)",
                             "(tt as u128) > (now.now_real as u128)"),
    "earliest ignores lead": ("Timelock::Time(tt) => (tt - b.max_lead_secs) as u128,",
                              "Timelock::Time(tt) => tt as u128,"),
    "latest ignores lag": ("Timelock::Time(tt) => (tt as u128) + (b.max_lag_secs as u128),",
                           "Timelock::Time(tt) => tt as u128,"),
    "earliest uses slowest block": ("(blocks as u128) * (b.min_block_secs as u128)",
                                    "(blocks as u128) * (b.max_block_secs as u128)"),
    "latest uses fastest block": ("(blocks as u128) * (b.max_block_secs as u128)",
                                  "(blocks as u128) * (b.min_block_secs as u128)"),
    "s11 drops margin": ("(d_observe as u128) + (d_confirm as u128) + (d_margin as u128)",
                         "(d_observe as u128) + (d_confirm as u128)"),
    "s11 accepts not pending": ("if !(pending_exec(ta, now_a, ba) && pending_exec(tb, now_b, bb)) {\n        return false;",
                                "if !(pending_exec(ta, now_a, ba) && pending_exec(tb, now_b, bb)) {\n        return true;"),
    "s12 drops confirmation": ("(now_b.now_real as u128) + (d_confirm as u128) + (d_margin as u128) <= earliest_exec",
                               "(now_b.now_real as u128) + (d_margin as u128) <= earliest_exec"),
    "gap clamps up": ("if later <= earlier {\n        0", "if later <= earlier {\n        1"),
}

# Genuine verification failures; compiler or setup errors do not count.
PROOF_FAILURES = ("postcondition not satisfied", "invariant not satisfied", "precondition not satisfied",
                  "assertion failed", "possible arithmetic underflow/overflow")


def verify(verus, path):
    return subprocess.run(
        [verus, "--crate-type", "lib", "--edition", "2021", "--no-cheating",
         "--num-threads", "2", str(path)],
        capture_output=True, text=True, timeout=300,
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
        raise SystemExit("Swap evaluator verification failed")
    print("Verified source SHA-256:", hashlib.sha256(SOURCE.read_bytes()).hexdigest(), flush=True)
    if not args.mutations:
        return

    source = SOURCE.read_text()
    prefix, executable = source.split(MARKER, 1)
    with tempfile.TemporaryDirectory(prefix="warrant-swap-verus-mutations-") as directory:
        for name, (before, after) in MUTATIONS.items():
            if executable.count(before) != 1:
                raise SystemExit(f"Mutation needs updating: {name}")
            path = Path(directory) / "mutant.rs"
            path.write_text(prefix + MARKER + executable.replace(before, after, 1))
            result = verify(verus, path)
            output = result.stdout + result.stderr
            if result.returncode == 0 or not any(failure in output for failure in PROOF_FAILURES):
                print(output)
                raise SystemExit(f"Mutation did not produce the expected proof failure: {name}")
            print(f"Rejected mutation: {name}", flush=True)
    print(f"All {len(MUTATIONS)} executable mutations rejected.")


if __name__ == "__main__":
    main()
