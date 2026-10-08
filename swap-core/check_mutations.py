#!/usr/bin/env python3
"""Disable each obligatory check in turn and require that some test fails.

This shows that every check in src/checks.rs is load-bearing: the tests pass
with it and fail without it."""
import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "src" / "checks.rs"
# S1 is also enforced by the type: `HashAlg` has only `Sha256`, so a lock with another
# hash function cannot be parsed (test `types::tests::other_hash_functions_do_not_parse`).
# S19 is also enforced by `validate_policy`, which rejects a profile without a fee-raise method.
CHECKS = ["s2", "s3", "s4", "s5_s6_terms", "observed_lock", "s7", "s8_flags", "s10",
          "s10_lock_id", "s10_lock_binding", "timelock_form", "leg_b_absolute", "s11", "s13_timelock", "s12", "s14", "s15",
          "s16", "s17", "s21", "s22", "s23", "s27"]


def main():
    original = SOURCE.read_text()
    survived = []
    try:
        for name in CHECKS:
            pattern = re.compile(r"(pub fn " + name + r"\b[^{]*\{)")
            if len(pattern.findall(original)) != 1:
                sys.exit(f"mutation needs updating: {name}")
            SOURCE.write_text(pattern.sub(r"\1\n    #[allow(unreachable_code)]\n    return Ok(());", original, count=1))
            result = subprocess.run(["cargo", "test", "-q", "-p", "warrant-swap-core"], capture_output=True, text=True)
            output = result.stdout + result.stderr
            if result.returncode == 0:
                survived.append(name)
                print(f"SURVIVED: {name}", flush=True)
            elif "test result: FAILED" not in output:
                print(output)
                sys.exit(f"build failure, not a test failure: {name}")
            else:
                print(f"Caught: {name}", flush=True)
    finally:
        SOURCE.write_text(original)
    if survived:
        sys.exit(f"checks without a failing test: {survived}")
    print(f"All {len(CHECKS)} disabled checks caught by a test.")


if __name__ == "__main__":
    main()
