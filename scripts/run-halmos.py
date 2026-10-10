#!/usr/bin/env python3
"""Run pinned Halmos with its SHA256 byte/bit width bug corrected in memory.

Halmos 0.3.3 sevm.py gives a 480-byte precompile input a 480-bit sort, then
passes 3840 bits: Z3 rejects every invoice path before checking assertions.
This narrow compatibility fix changes only the argument sort to bytes * 8.
SHA256 remains Halmos's uninterpreted function, not a proof of the hash itself.
No installed package or contract bytecode is modified.
"""
import importlib.metadata
from pathlib import Path
import sys

if importlib.metadata.version("halmos") != "0.3.3":
    raise SystemExit("Expected halmos==0.3.3; re-review the compatibility fix before upgrading")
import halmos.sevm

source = Path(halmos.sevm.__file__).read_text()
old = 'f"f_sha256_{arg_size}", BitVecSorts[arg_size], BitVecSort256'
new = 'f"f_sha256_{arg_size}", BitVecSorts[arg_size * 8], BitVecSort256'
if source.count(old) != 1:
    raise SystemExit("Unexpected Halmos SHA256 implementation; refusing to run")
exec(compile(source.replace(old, new), halmos.sevm.__file__, "exec"), halmos.sevm.__dict__)
from halmos.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
