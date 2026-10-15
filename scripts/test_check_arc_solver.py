"""Guard tests for check-arc-solver.py. They mock every external command, so no Foundry or chain is needed.

Run from the repository root: .venv/bin/python -m unittest scripts.test_check_arc_solver -v
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("probe", pathlib.Path(__file__).with_name("check-arc-solver.py"))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

ESCROW, VERIFIER = "0x" + "a" * 40, "0x" + "b" * 40
IMAGE = "0x" + "1" * 64
PROBE_CODE = "0x6080"
CHAIN_CODE = "0x" + "cd" * 40
HASH_OK = "0x" + "e" * 64
RPC = "https://rpc.example/KEY123"
MANIFEST = dict(escrow=ESCROW, verifier=VERIFIER, image_id=IMAGE, token=probe.ARC_USDC,
                code_hashes=dict(escrow=HASH_OK, verifier=HASH_OK))


def precompile_results():
    return {address: expected for _, address, _, expected in probe.PRECOMPILES}


class FakeChain:
    """Answers the read-only commands the probe is allowed to run. Any other command is an error."""

    def __init__(self, **change):
        self.chain_id = "5042"
        self.decimals = "6"
        self.precompiles = precompile_results()
        self.probe_result = probe.WORD_ONE
        self.probe_error = None
        self.code = {ESCROW: CHAIN_CODE, VERIFIER: CHAIN_CODE}
        self.getters = {"token()(address)": probe.ARC_USDC, "verifier()(address)": VERIFIER,
                        "imageId()(bytes32)": IMAGE, "totalReserved()(uint256)": "0"}
        self.commands = []
        self.__dict__.update(change)

    def __call__(self, *args, cwd=None):
        args = [str(a) for a in args]
        self.commands.append(args)
        head = tuple(args[:2])
        if head == ("cast", "chain-id"):
            return self.chain_id
        if head == ("forge", "build"):
            return ""
        if head == ("forge", "inspect"):
            return PROBE_CODE
        if head == ("cast", "abi-encode"):
            return "0x" + "00" * 32
        if head == ("cast", "keccak"):
            return HASH_OK
        if head == ("cast", "code"):
            return self.code[args[2]]
        if head == ("cast", "call"):
            if args[3] == "decimals()(uint8)":
                return self.decimals
            return self.getters[args[3]]
        if head == ("cast", "rpc"):
            request = json.loads(args[3])
            if "to" in request:
                return json.dumps(self.precompiles[request["to"]])
            if self.probe_error:
                raise probe.ProbeError(self.probe_error)
            return json.dumps(self.probe_result)
        raise AssertionError(f"unexpected command {args}")


def artifact(immutables):
    return {"deployedBytecode": {"object": CHAIN_CODE, "immutableReferences": immutables}}


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.proof = self.root / "evm.json"
        self.proof.write_text(json.dumps(dict(seal="0x01", imageId=IMAGE, journalDigest="0x" + "2" * 64)))
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps(MANIFEST))
        self.out = self.root / "report.json"
        self.chain = FakeChain()
        for target in (patch.object(probe, "run", self.chain),
                       patch.object(probe, "load_artifact", lambda name: artifact({})),
                       patch.dict(os.environ, {probe.RPC_ENV: RPC}, clear=False)):
            target.start()
            self.addCleanup(target.stop)
        os.environ.pop(probe.RISC0_DEV_MODE, None)

    def before(self, *extra):
        return probe.main(["before", str(self.proof), "--out", str(self.out), *extra])

    def after(self, *extra):
        return probe.main(["after", str(self.manifest), "--out", str(self.out), *extra])

    def report(self):
        return json.loads(self.out.read_text())

    def assertOnlyReadOnlyCommands(self):
        for command in self.chain.commands:
            self.assertNotIn("send", command)
            self.assertNotIn("--broadcast", command)
            self.assertNotIn("script", command)
            self.assertNotIn(RPC, " ".join(command))


class BeforeTests(ProbeCase):
    def test_success_writes_report_and_exits_zero(self):
        self.assertEqual(self.before(), 0)
        report = self.report()
        self.assertEqual((report["verdict"], report["chain_id"], report["problems"]), ("ok", 5042, []))
        self.assertTrue(report["probe"]["ok"])
        self.assertTrue(all(entry["ok"] for entry in report["precompiles"].values()))
        self.assertOnlyReadOnlyCommands()

    def test_wrong_chain_is_refused_before_any_other_call(self):
        self.chain.chain_id = "5042002"
        self.assertEqual(self.before(), 1)
        self.assertIn("chain 5042002", self.report()["problems"][0])
        self.assertEqual([c[:2] for c in self.chain.commands], [["cast", "chain-id"]])

    def test_allow_chain_accepts_a_local_rehearsal_chain(self):
        self.chain.chain_id = "31337"
        self.assertEqual(self.before("--allow-chain", "31337"), 0)
        encode = next(c for c in self.chain.commands if c[:2] == ["cast", "abi-encode"])
        self.assertEqual(encode[-1], "31337")

    def test_mainnet_passes_zero_as_the_allowed_chain(self):
        self.before()
        encode = next(c for c in self.chain.commands if c[:2] == ["cast", "abi-encode"])
        self.assertEqual(encode[-1], "0")

    def test_precompile_result_mismatch_is_reported(self):
        ecmul = probe.PRECOMPILES[1][1]
        self.chain.precompiles[ecmul] = "0x" + "00" * 64
        self.assertEqual(self.before(), 1)
        report = self.report()
        self.assertFalse(report["precompiles"]["ecmul"]["ok"])
        self.assertTrue(any("ecmul" in p for p in report["problems"]))

    def test_decimals_mismatch_is_reported(self):
        self.chain.decimals = "18"
        self.assertEqual(self.before(), 1)
        self.assertTrue(any("18 decimals" in p for p in self.report()["problems"]))

    def test_probe_revert_is_reported(self):
        self.chain.probe_error = "execution reverted: Changed journal accepted"
        self.assertEqual(self.before(), 1)
        report = self.report()
        self.assertFalse(report["probe"]["ok"])
        self.assertTrue(any("Changed journal accepted" in p for p in report["problems"]))

    def test_probe_unexpected_return_is_reported(self):
        self.chain.probe_result = "0x" + "00" * 32
        self.assertEqual(self.before(), 1)
        self.assertFalse(self.report()["probe"]["ok"])

    def test_dev_mode_is_refused(self):
        with patch.dict(os.environ, {probe.RISC0_DEV_MODE: "1"}):
            self.assertEqual(self.before(), 1)
        self.assertTrue(any(probe.RISC0_DEV_MODE in p for p in self.report()["problems"]))

    def test_image_id_argument_must_match_the_proof(self):
        self.assertEqual(self.before("--image-id", "0x" + "9" * 64), 1)
        self.assertTrue(any("--image-id" in p for p in self.report()["problems"]))


class AfterTests(ProbeCase):
    def test_success_checks_hashes_getters_and_reservation(self):
        self.assertEqual(self.after(), 0)
        report = self.report()
        self.assertEqual(report["verdict"], "ok")
        self.assertEqual(report["escrow"]["totalReserved"], 0)
        self.assertTrue(report["code"]["escrow"]["matches_build"])
        self.assertOnlyReadOnlyCommands()

    def test_manifest_hash_mismatch_is_reported(self):
        self.manifest.write_text(json.dumps(dict(MANIFEST, code_hashes=dict(escrow="0x" + "f" * 64,
                                                                           verifier=HASH_OK))))
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("escrow code hash" in p for p in self.report()["problems"]))

    def test_build_mismatch_outside_immutables_is_reported(self):
        self.chain.code[VERIFIER] = "0x" + "cd" * 39 + "ff"
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("RiscZeroGroth16Verifier code on chain differs" in p for p in self.report()["problems"]))

    def test_immutable_slots_are_masked_in_the_build_comparison(self):
        self.chain.code[ESCROW] = "0x" + "cd" * 10 + "00" * 20 + "cd" * 10
        with patch.object(probe, "load_artifact", lambda name: artifact({"1": [{"start": 10, "length": 20}]})):
            self.assertEqual(self.after(), 0)

    def test_funded_escrow_is_refused(self):
        self.chain.getters["totalReserved()(uint256)"] = "2000000"
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("already funded" in p for p in self.report()["problems"]))

    def test_getter_mismatch_is_reported(self):
        self.chain.getters["imageId()(bytes32)"] = "0x" + "3" * 64
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("escrow.imageId()" in p for p in self.report()["problems"]))

    def test_missing_code_is_reported(self):
        self.chain.code[ESCROW] = probe.EMPTY_CODE
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("no code at the escrow" in p for p in self.report()["problems"]))

    def test_missing_manifest_keys_are_reported(self):
        self.manifest.write_text(json.dumps({"escrow": ESCROW}))
        self.assertEqual(self.after(), 1)
        problems = self.report()["problems"]
        self.assertTrue(any("'verifier'" in p for p in problems) and any("'image_id'" in p for p in problems))

    def test_wrong_chain_is_refused(self):
        self.chain.chain_id = "1"
        self.assertEqual(self.after(), 1)
        self.assertNotIn("code", self.report())

    def test_manifest_with_an_order_transaction_is_refused(self):
        self.manifest.write_text(json.dumps(dict(MANIFEST, transactions={"deploy": "0x" + "4" * 64,
                                                                        "offer": "0x" + "5" * 64})))
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("records a offer transaction" in p for p in self.report()["problems"]))
        self.assertNotIn("code", self.report())

    def write_state(self, tx):
        """A run state.json as solver-canary.py writes it: steps live under tx, there is no transactions key."""
        state = dict(MANIFEST, version=1, chain_id=5042, amount=2_000_000, tx=tx,
                     addresses={"deployer": "0x" + "1" * 40, "buyer": "0x" + "2" * 40})
        self.manifest.write_text(json.dumps(state))

    def test_run_state_with_tx_is_accepted(self):
        self.write_state({"deploy": {"intent_at": "t", "hash": "see deployment.json"}})
        self.assertEqual(self.after(), 0)
        self.assertEqual(self.report()["verdict"], "ok")

    def test_order_step_in_tx_is_refused(self):
        self.write_state({"deploy": {"intent_at": "t", "hash": "see deployment.json"},
                          "offer": {"intent_at": "t", "hash": "0x" + "5" * 64}})
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("records a offer transaction" in p for p in self.report()["problems"]))
        self.assertNotIn("code", self.report())

    def test_unconfirmed_order_intent_in_tx_is_refused(self):
        self.write_state({"approve": {"intent_at": "t"}})
        self.assertEqual(self.after(), 1)
        self.assertTrue(any("records a approve transaction" in p for p in self.report()["problems"]))
        self.assertNotIn("code", self.report())

    def test_reused_run_state_with_empty_tx_is_accepted(self):
        self.write_state({})
        self.assertEqual(self.after(), 0)
        report = self.report()
        self.assertEqual((report["verdict"], report["problems"]), ("ok", []))


class RunTests(unittest.TestCase):
    def test_errors_do_not_leak_the_rpc_url(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=f"cannot reach {RPC}")
        with patch.dict(os.environ, {probe.RPC_ENV: RPC}), patch.object(probe.subprocess, "run", return_value=failed):
            with self.assertRaises(probe.ProbeError) as ctx:
                probe.run("cast", "chain-id")
        self.assertNotIn("KEY123", str(ctx.exception))
        self.assertIn("<rpc>", str(ctx.exception))

    def test_rpc_url_goes_in_the_child_environment_only(self):
        ok = subprocess.CompletedProcess([], 0, stdout="5042\n", stderr="")
        with patch.dict(os.environ, {probe.RPC_ENV: RPC}), \
                patch.object(probe.subprocess, "run", return_value=ok) as run:
            probe.run("cast", "chain-id")
        self.assertNotIn("KEY123", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["env"]["ETH_RPC_URL"], RPC)

    def test_rpc_url_must_be_https_or_loopback(self):
        for url in ("", "http://example.com", "ftp://x"):
            with patch.dict(os.environ, {probe.RPC_ENV: url}), self.assertRaises(probe.ProbeError):
                probe.rpc_url()


class ReportTests(ProbeCase):
    def test_report_file_is_never_overwritten(self):
        self.out.write_text("keep me\n")
        self.assertEqual(self.before(), 1)
        self.assertEqual(self.out.read_text(), "keep me\n")

    def test_journal_digest_falls_back_to_sha256_of_the_journal(self):
        self.proof.write_text(json.dumps(dict(seal="0x01", imageId=IMAGE, journal="0xabcd")))
        self.assertEqual(self.before(), 0)
        self.assertEqual(self.report()["proof"]["journal_digest"],
                         "0x" + __import__("hashlib").sha256(bytes.fromhex("abcd")).hexdigest())


if __name__ == "__main__":
    unittest.main()
