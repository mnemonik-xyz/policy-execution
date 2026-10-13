"""Guard tests for check-arc.py. Every external command is mocked; no Foundry or chain is needed.

Run from the repository root: python3 scripts/test_check_arc.py
"""
import argparse
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("check_arc", pathlib.Path(__file__).with_name("check-arc.py"))
check_arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_arc)

TOKEN = bytes.fromhex("36" + "00" * 19)
VERIFIER = bytes.fromhex("ab" * 20)
IMAGE = bytes.fromhex("cd" * 32)
ROOT0, ROOT1, BN254 = bytes.fromhex("11" * 16), bytes.fromhex("22" * 16), bytes.fromhex("33" * 32)


def with_immutables(local, placements):
    code = bytearray(local)
    for offset, value in placements:
        code[offset:offset + 32] = value
    return bytes(code)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.local = bytes(range(256)) * 2 + b"\0" * 32 + bytes(range(1, 101)) + b"\0" * 32 + b"\xfe" * 7
        self.words = [check_arc.word("0x" + TOKEN.hex()), check_arc.word("0x" + VERIFIER.hex()), IMAGE]

    def test_identical_code_matches(self):
        self.assertTrue(check_arc.compare_runtime_code(self.local, self.local, self.words))

    def test_immutable_words_may_differ(self):
        deployed = with_immutables(self.local, [(512, self.words[0]), (644, IMAGE)])
        self.assertTrue(check_arc.compare_runtime_code(deployed, self.local, self.words))

    def test_unexpected_word_fails(self):
        deployed = with_immutables(self.local, [(512, b"\x99" * 32)])
        self.assertFalse(check_arc.compare_runtime_code(deployed, self.local, self.words))

    def test_difference_outside_a_zero_window_fails(self):
        deployed = bytearray(self.local)
        deployed[3] ^= 1
        self.assertFalse(check_arc.compare_runtime_code(bytes(deployed), self.local, self.words))
        shifted = with_immutables(self.local, [(513, IMAGE)])  # not aligned with the zero window
        self.assertFalse(check_arc.compare_runtime_code(shifted, self.local, self.words))

    def test_length_or_metadata_change_fails(self):
        self.assertFalse(check_arc.compare_runtime_code(self.local + b"\0", self.local, self.words))
        tail = bytearray(self.local)
        tail[-1] ^= 1
        self.assertFalse(check_arc.compare_runtime_code(bytes(tail), self.local, self.words))

    def test_word_alignment(self):
        self.assertEqual(check_arc.word("0x" + TOKEN.hex()), b"\0" * 12 + TOKEN)
        self.assertEqual(check_arc.word("0x" + ROOT0.hex()), ROOT0 + b"\0" * 16)
        self.assertEqual(check_arc.word("0x" + IMAGE.hex()), IMAGE)
        with self.assertRaises(check_arc.CheckError):
            check_arc.word("0x0102")


class ChainTests(unittest.TestCase):
    def test_networks(self):
        self.assertEqual(check_arc.NETWORKS["mainnet"]["chain_id"], 5042)
        self.assertEqual(check_arc.NETWORKS["testnet"]["chain_id"], 5042002)

    def test_require_chain_rejects_the_other_network(self):
        with patch.object(check_arc, "chain_id", return_value=5042002):
            self.assertEqual(check_arc.require_chain("rpc", "testnet"), 5042002)
            with self.assertRaises(check_arc.CheckError):
                check_arc.require_chain("rpc", "mainnet")

    def test_control_ids_are_parsed_from_the_vendored_source(self):
        root0, root1, bn254 = check_arc.control_ids()
        self.assertEqual((len(root0), len(root1), len(bn254)), (16, 16, 32))
        self.assertEqual(root0 + root1,
                         bytes.fromhex("a54dc85ac99f851c92d7c96d7318af41dbe7c0194edfcc37eb4d422a998c1f56"))

    def test_probe_requires_the_constructor_to_return_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            proof = pathlib.Path(tmp) / "proof.json"
            zero = "0x" + "00" * 32
            proof.write_text(json.dumps({"seal": "0x01", "imageId": zero, "journalDigest": zero}))
            args = argparse.Namespace(network="mainnet", rpc=None, proof=str(proof))
            with patch.object(check_arc, "require_chain", return_value=5042), \
                    patch.object(check_arc, "check_usdc"), patch.object(check_arc, "check_precompiles"), \
                    patch.object(check_arc, "run", return_value="0x00"), \
                    patch.object(check_arc, "rpc_call", return_value="0x" + "00" * 32):
                with self.assertRaises(check_arc.CheckError):
                    check_arc.cmd_probe(args)


class VerifyTests(unittest.TestCase):
    def test_verify_compares_immutables_and_writes_the_manifest_once(self):
        local_escrow = b"\xaa" * 40 + b"\0" * 32 + b"\xbb" * 8 + b"\0" * 32 + b"\0" * 32 + b"\xcc" * 5
        deployed_escrow = with_immutables(local_escrow, [(40, check_arc.word("0x" + TOKEN.hex())),
                                                         (80, check_arc.word("0x" + VERIFIER.hex())),
                                                         (112, IMAGE)])
        local_verifier = b"\xdd" * 3 + b"\0" * 32 + b"\0" * 32 + b"\0" * 32 + b"\xee" * 9
        deployed_verifier = with_immutables(local_verifier, [(3, ROOT0 + b"\0" * 16), (35, ROOT1 + b"\0" * 16),
                                                             (67, BN254)])
        found = {"token": TOKEN, "verifier": VERIFIER, "image_id": IMAGE, "control_root_0": ROOT0,
                 "control_root_1": ROOT1, "bn254_control_id": BN254}
        code = {"0x" + VERIFIER.hex(): deployed_verifier, "0x" + "ee" * 20: deployed_escrow}
        inspected = {"TaskEscrow": local_escrow, "RiscZeroGroth16Verifier": local_verifier}

        def fake_run(*command, cwd=None):
            command = [str(c) for c in command]
            if command[:2] == ["cast", "code"]:
                return "0x" + code[command[2]].hex()
            if command[:2] == ["forge", "inspect"]:
                return "0x" + inspected[command[2]].hex()
            if command[:2] == ["cast", "keccak"]:
                return "0x" + "77" * 32
            if command[:2] == ["git", "rev-parse"]:
                return "deadbeef"
            if command[:2] == ["git", "status"]:
                return ""
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "manifest.json"
            args = argparse.Namespace(network="mainnet", rpc=None, escrow="0x" + "ee" * 20,
                                      verifier="0x" + VERIFIER.hex(), image_id="0x" + IMAGE.hex(), out=str(out))
            with patch.object(check_arc, "require_chain", return_value=5042), \
                    patch.object(check_arc, "control_ids", return_value=(ROOT0, ROOT1, BN254)), \
                    patch.object(check_arc, "read_immutables", return_value=found), \
                    patch.object(check_arc, "broadcast_transactions", return_value=None), \
                    patch.object(check_arc, "run", side_effect=fake_run):
                check_arc.cmd_verify(args)
                manifest = json.loads(out.read_text())
                self.assertEqual((manifest["chain_id"], manifest["source_commit"]), (5042, "deadbeef"))
                self.assertEqual(manifest["compiler"]["solc"], "0.8.28")
                self.assertEqual(manifest["escrow"]["image_id"], "0x" + IMAGE.hex())
                with self.assertRaises(FileExistsError):
                    check_arc.cmd_verify(args)
                wrong = dict(found, image_id=b"\x01" * 32)
                with patch.object(check_arc, "read_immutables", return_value=wrong), \
                        self.assertRaises(check_arc.CheckError) as ctx:
                    check_arc.cmd_verify(argparse.Namespace(**dict(vars(args), out=str(out) + ".2")))
                self.assertIn("image_id", str(ctx.exception))
                code["0x" + "ee" * 20] = deployed_escrow[:-1] + b"\x00"
                with self.assertRaises(check_arc.CheckError) as ctx:
                    check_arc.cmd_verify(argparse.Namespace(**dict(vars(args), out=str(out) + ".3")))
                self.assertIn("TaskEscrow code differs", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
