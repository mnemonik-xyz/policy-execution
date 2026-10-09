"""Guard tests for solver-canary.py. They mock every external command, so no Foundry or chain is needed.

Run from the repository root: python3 scripts/test_solver_canary.py
"""
import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("canary", pathlib.Path(__file__).with_name("solver-canary.py"))
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)

A, B, C, D = ("0x" + str(n) * 40 for n in (1, 2, 3, 4))


def init_args(directory, **change):
    values = dict(dir=directory, chain_id=5042, token=canary.ARC_USDC, amount=2_000_000, max_cost=14,
                  instance=None, accept_window=3600, settle_window=14400,
                  deployer=f"ledger:{A}", buyer=f"ledger:{B}", seller=f"ledger:{C}", relayer=f"ledger:{D}")
    values.update(change)
    return argparse.Namespace(**values)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.instance = self.root / "instance.json"
        self.instance.write_text("{}")

    def init(self, name="run", **change):
        args = init_args(str(self.root / name), instance=str(self.instance), **change)
        canary.cmd_init(args)
        return args.dir


class InitTests(TempCase):
    def test_amount_cap_and_bad_inputs_are_refused(self):
        for change in ({"amount": 0}, {"amount": canary.HARD_CAP + 1}, {"token": "0x12"},
                       {"settle_window": 3600}, {"seller": f"ledger:{B}"}, {"relayer": f"ledger:{A}"},
                       {"deployer": f"unlocked:{A}"}, {"buyer": "key:0xabc"}):
            with self.subTest(change=change), self.assertRaises(canary.CanaryError):
                self.init(name="bad", **change)

    def test_valid_init_saves_state_and_refuses_reuse(self):
        directory = self.init()
        state = canary.load(directory)
        self.assertEqual((state["chain_id"], state["amount"], state["tx"]), (5042, 2_000_000, {}))
        with self.assertRaises(FileExistsError):
            self.init()

    def test_unlocked_signers_are_allowed_off_mainnet(self):
        directory = self.init(name="local", chain_id=31337, deployer=f"unlocked:{A}", buyer=f"unlocked:{B}",
                              seller=f"unlocked:{C}", relayer=f"unlocked:{D}")
        self.assertEqual(canary.load(directory)["chain_id"], 31337)


class StepTests(TempCase):
    def setUp(self):
        super().setUp()
        self.dir = self.init()
        self.st = canary.load(self.dir)

    def args(self, **change):
        return argparse.Namespace(**dict(dict(execute=False, confirm_chain=None, confirm_amount=None), **change))

    def test_confirmation_must_match_state(self):
        self.assertFalse(canary.confirmed(self.st, self.args()))
        for wrong in (dict(confirm_chain=1, confirm_amount=2_000_000), dict(confirm_chain=5042, confirm_amount=1)):
            with self.assertRaises(canary.CanaryError):
                canary.confirmed(self.st, self.args(execute=True, **wrong))
        right = self.args(execute=True, confirm_chain=5042, confirm_amount=2_000_000)
        self.assertTrue(canary.confirmed(self.st, right))

    def test_dry_run_never_broadcasts(self):
        calls = []
        canary.step(self.dir, self.st, "offer", self.args(), lambda: calls.append("sim"), lambda: calls.append("send"))
        self.assertEqual(calls, ["sim"])
        self.assertNotIn("offer", canary.load(self.dir)["tx"])

    def test_intent_is_saved_before_broadcast_and_never_retried(self):
        go = self.args(execute=True, confirm_chain=5042, confirm_amount=2_000_000)

        def broadcast():
            self.assertIn("intent_at", canary.load(self.dir)["tx"]["offer"])
            raise canary.CanaryError("network lost")

        with self.assertRaises(canary.CanaryError):
            canary.step(self.dir, self.st, "offer", go, lambda: None, broadcast)
        with self.assertRaises(canary.CanaryError) as ctx:
            canary.step(self.dir, canary.load(self.dir), "offer", go, lambda: None, lambda: {"hash": "0x1"})
        self.assertIn("already attempted", str(ctx.exception))

    def test_settle_refuses_when_substituted_result_passes_simulation(self):
        directory = pathlib.Path(self.dir)
        (directory / "evm.json").write_text(json.dumps(dict(seal="0x01", journal="0x02")))
        self.st.update(proof={}, escrow=A, quote={"task": "0x" + "9" * 64},
                       delivery=dict(result="0xaa00", result_hash="0x1"))
        canary.save(self.dir, self.st)
        with patch.object(canary, "connect"), patch.object(canary, "call", return_value="ok"):
            with self.assertRaises(canary.CanaryError) as ctx:
                canary.cmd_settle(argparse.Namespace(dir=self.dir, execute=False, confirm_chain=None,
                                                     confirm_amount=None))
        self.assertIn("substituted", str(ctx.exception))


class RunTests(unittest.TestCase):
    def test_rpc_url_must_be_https_or_loopback(self):
        for url in ("", "http://example.com", "ftp://x"):
            with patch.dict(os.environ, {canary.RPC_ENV: url}), self.assertRaises(canary.CanaryError):
                canary.rpc_url()
        with patch.dict(os.environ, {canary.RPC_ENV: "http://127.0.0.1:8545"}):
            self.assertEqual(canary.rpc_url(), "http://127.0.0.1:8545")

    def test_errors_do_not_leak_the_rpc_url(self):
        secret = "https://rpc.example/KEY123"
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=f"cannot reach {secret}")
        with patch.dict(os.environ, {canary.RPC_ENV: secret}), \
                patch.object(canary.subprocess, "run", return_value=failed):
            with self.assertRaises(canary.CanaryError) as ctx:
                canary.run("cast", "chain-id")
        self.assertNotIn("KEY123", str(ctx.exception))

    def test_rpc_url_goes_in_the_child_environment_only(self):
        ok = subprocess.CompletedProcess([], 0, stdout="5042\n", stderr="")
        with patch.dict(os.environ, {canary.RPC_ENV: "https://rpc.example/KEY"}), \
                patch.object(canary.subprocess, "run", return_value=ok) as run:
            canary.run("cast", "chain-id")
        self.assertNotIn("KEY", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["env"]["ETH_RPC_URL"], "https://rpc.example/KEY")


if __name__ == "__main__":
    unittest.main()
