"""Guard tests for solver-canary.py. They mock every external command, so no Foundry or chain is needed.

Run from the repository root: python3 scripts/test_solver_canary.py
"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("canary", pathlib.Path(__file__).with_name("solver-canary.py"))
canary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canary)

A, B, C, D = ("0x" + str(n) * 40 for n in (1, 2, 3, 4))
ESCROW, VERIFIER, SHOWCASE = ("0x" + str(n) * 40 for n in (5, 6, 7))
TASK = "0x" + "9" * 64
REFUNDED_TOPIC = "0x" + "f0" * 32
CODE_HASHES = {"escrow": "0x" + "a1" * 32, "verifier": "0x" + "b2" * 32}
IMAGE_ID = "0x" + "ab" * 32
PASSWORD_FILE = "/run/warrant-canary/pw/buyer"


def fresh_canary():
    """Load a new copy of the script, so that ENV is built from the os.environ of the calling test."""
    spec = importlib.util.spec_from_file_location("canary_fresh", pathlib.Path(__file__).with_name("solver-canary.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def init_args(directory, **change):
    values = dict(dir=directory, chain_id=5042, token=canary.ARC_USDC, amount=2_000_000, max_cost=14,
                  instance=None, accept_window=3600, settle_window=14400, reuse_deployment=None,
                  deployer=f"ledger:{A}", buyer=f"ledger:{B}", seller=f"ledger:{C}", relayer=f"ledger:{D}")
    values.update(change)
    return argparse.Namespace(**values)


class CanaryChain:
    """Answers the cast commands that refund, accept and init --reuse-deployment run. Any other command fails.

    A cast send of refund(bytes32) applies the refund: the task state becomes state_after, the escrow balance falls by
    escrow_drop and the buyer balance rises by buyer_gain. The receipt carries the configured gas figures and logs.
    """

    def __init__(self, st, **change):
        self.st = st
        self.chain_id = st["chain_id"]
        self.now = 0
        self.state, self.state_after = 1, 4
        self.escrow_drop = self.buyer_gain = st["amount"]
        self.gas_used, self.gas_price = 100_000, 20 * 10**9
        self.logs = None
        self.code_hashes = dict(CODE_HASHES)
        self.image_id, self.token, self.verifier = IMAGE_ID, st["token"], VERIFIER
        self.buyer_onchain, self.amount_onchain = st["addresses"]["buyer"], st["amount"]
        self.probe_problems, self.probes = [], []
        self.on_send = None
        self.balances = {ESCROW: st["amount"], st["addresses"]["buyer"]: 1_000_000}
        self.commands, self.simulated, self.sent = [], [], []
        self.__dict__.update(change)

    def terms(self):
        q, a = self.st["quote"], self.st["addresses"]
        values = [self.buyer_onchain, a["seller"], "0x" + "8" * 64, "1", f"{self.amount_onchain} [2e6]",
                  str(q["accept_by"]), str(q["settle_by"]), str(self.state)]
        return "\n".join(values)

    def __call__(self, *args, cwd=None, interactive=False, stdin=None):
        args = [str(a) for a in args]
        self.commands.append(args)
        if args[0] == str(canary.NATIVE):
            return "0x" + "8" * 64
        if args[0] == sys.executable and args[1].endswith("check-arc-solver.py"):
            return self.probe(args)
        head = tuple(args[:2])
        if head == ("cast", "chain-id"):
            return str(self.chain_id)
        if head == ("cast", "block"):
            return str(self.now)
        if head == ("cast", "code"):
            return "0xc0de" + args[2][2:]
        if head == ("cast", "keccak"):
            if args[2] == "Refunded(bytes32)":
                return REFUNDED_TOPIC
            return self.code_hashes["escrow" if ESCROW[2:] in args[2] else "verifier"]
        if head == ("cast", "call"):
            return self.answer_call(args)
        if head == ("cast", "send"):
            return self.send(args)
        raise AssertionError(f"unexpected command {args}")

    def answer_call(self, args):
        signature = args[3]
        if signature.startswith("tasks(bytes32)"):
            return self.terms()
        if signature == "balanceOf(address)(uint256)":
            return f"{self.balances.get(args[4].lower(), 0)} [1e6]"
        if signature == "imageId()(bytes32)":
            return self.image_id
        if signature == "token()(address)":
            return self.token
        if signature == "verifier()(address)":
            return self.verifier
        if signature in ("refund(bytes32)", "accept(bytes32)", "approve(address,uint256)(bool)"):
            self.simulated.append((signature, args[args.index("--from") + 1]))
            return "0x"
        raise AssertionError(f"unexpected call {args}")

    def probe(self, args):
        """The read-only probe: record its input and write a report, or fail as the real probe does."""
        self.probes.append((args, json.loads(pathlib.Path(args[3]).read_text())))
        verdict = "failed" if self.probe_problems else "ok"
        pathlib.Path(args[args.index("--out") + 1]).write_text(
            json.dumps({"phase": "after", "verdict": verdict, "problems": self.probe_problems}))
        if self.probe_problems:
            raise canary.CanaryError("python3 after failed:\n" + "\n".join(self.probe_problems))
        return f"after: {verdict}"

    def send(self, args):
        self.sent.append(args)
        if self.on_send:
            self.on_send()
        self.state = self.state_after
        self.balances[ESCROW] -= self.escrow_drop
        self.balances[self.st["addresses"]["buyer"]] += self.buyer_gain
        logs = self.logs if self.logs is not None else [
            {"address": ESCROW, "topics": [REFUNDED_TOPIC, TASK], "data": "0x"}]
        return json.dumps({"status": "0x1", "transactionHash": "0x" + "4" * 64, "blockNumber": "0x10",
                           "gasUsed": hex(self.gas_used), "effectiveGasPrice": hex(self.gas_price), "logs": logs})


def spend_args(directory, **change):
    values = {"dir": directory, "role": "buyer", "execute": False, "confirm_chain": None, "confirm_amount": None}
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

    def ordered(self):
        """The state after a confirmed offer: the seller can now accept."""
        self.st.update(escrow=ESCROW, quote={"task": TASK, "salt": "0x" + "7" * 64, "policy_hash": "0x" + "8" * 64,
                                             "accept_by": 10_000, "settle_by": 20_000})
        self.st["tx"]["offer"] = {"intent_at": "t", "hash": "0x" + "3" * 64}
        canary.save(self.dir, self.st)

    def test_accept_refuses_within_120_seconds_of_accept_by(self):
        self.ordered()
        chain = CanaryChain(self.st, now=10_000 - 119)
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_accept(spend_args(self.dir, role=None))
        self.assertIn("Less than 120 s remain before acceptBy", str(ctx.exception))
        self.assertEqual(chain.simulated, [])
        self.assertNotIn("accept", canary.load(self.dir)["tx"])
        for now in (10_000 - 120, 10_000 - 121):  # exactly 120 s left is still allowed
            chain = CanaryChain(self.st, now=now)
            with self.subTest(now=now), patch.object(canary, "run", chain):
                canary.cmd_accept(spend_args(self.dir, role=None))
            self.assertEqual(chain.simulated, [("accept(bytes32)", C)])
            self.assertEqual(chain.sent, [])

    def test_accept_retry_is_reported_before_the_margin(self):
        self.ordered()
        self.st["tx"]["accept"] = {"intent_at": "t"}
        canary.save(self.dir, self.st)
        with patch.object(canary, "run", CanaryChain(self.st, now=10_000 - 1)), \
                self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_accept(spend_args(self.dir, role=None))
        self.assertIn("accept was already attempted", str(ctx.exception))

    def test_offer_refuses_within_120_seconds_of_accept_by(self):
        self.ordered()
        del self.st["tx"]["offer"]
        canary.save(self.dir, self.st)
        chain = CanaryChain(self.st, now=10_000 - 119)
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_offer(spend_args(self.dir, role=None))
        self.assertIn("too old to offer", str(ctx.exception))
        self.assertEqual(chain.simulated, [])
        for now in (10_000 - 120, 10_000 - 121):
            chain = CanaryChain(self.st, now=now)
            with self.subTest(now=now), patch.object(canary, "run", chain):
                canary.cmd_offer(spend_args(self.dir, role=None))
            self.assertEqual(chain.simulated, [("approve(address,uint256)(bool)", B)])
            self.assertEqual(chain.sent, [])
        self.assertEqual(canary.load(self.dir)["tx"], {})

    def test_accept_and_settle_refuse_after_refund(self):
        self.ordered()
        self.st["tx"]["refund"] = {"intent_at": "t", "hash": "0x" + "4" * 64}
        self.st.update(proof={}, delivery={"result": "0xaa00", "result_hash": "0x1"})
        canary.save(self.dir, self.st)
        before = canary.load(self.dir)["tx"]
        for command in (canary.cmd_accept, canary.cmd_settle):
            with self.subTest(command=command.__name__), patch.object(canary, "run") as run, \
                    patch.object(canary, "call") as call, self.assertRaises(canary.CanaryError) as ctx:
                command(spend_args(self.dir, role=None))
            self.assertIn("refunded", str(ctx.exception))
            self.assertIn("init --reuse-deployment", str(ctx.exception))
            run.assert_not_called()
            call.assert_not_called()
        self.assertEqual(canary.load(self.dir)["tx"], before)


class RefundTests(TempCase):
    """refund is a spending step: dry run first, explicit confirmations, intent before broadcast, no retry."""

    def ordered(self, chain_id=5042, token=canary.ARC_USDC, name="run"):
        if chain_id in canary.MAINNET_IDS:
            directory = self.init(name=name, token=token)
        else:
            signers = {role: f"unlocked:{addr}" for role, addr in zip(canary.ROLES, (A, B, C, D), strict=True)}
            directory = self.init(name=name, chain_id=chain_id, token=token, **signers)
        st = canary.load(directory)
        st.update(escrow=ESCROW, quote={"task": TASK, "accept_by": 10_000, "settle_by": 20_000})
        st["tx"]["offer"] = {"intent_at": "t", "hash": "0x" + "3" * 64}
        canary.save(directory, st)
        return directory, st

    def go(self, st, **change):
        return dict(execute=True, confirm_chain=st["chain_id"], confirm_amount=st["amount"], **change)

    def test_refund_dry_run_never_broadcasts(self):
        directory, st = self.ordered()
        chain = CanaryChain(st, state=1, now=5_000)
        with patch.object(canary, "run", chain):
            canary.cmd_refund(spend_args(directory))
        self.assertEqual(chain.simulated, [("refund(bytes32)", B)])
        self.assertEqual(chain.sent, [])
        self.assertNotIn("refund", canary.load(directory)["tx"])

    def test_refund_needs_matching_confirmations(self):
        directory, st = self.ordered()
        for wrong in ({"confirm_chain": 1}, {"confirm_amount": 1}):
            chain = CanaryChain(st, state=1, now=5_000)
            with self.subTest(wrong=wrong), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError):
                canary.cmd_refund(spend_args(directory, **dict(self.go(st), **wrong)))
            self.assertEqual(chain.sent, [])
            self.assertNotIn("refund", canary.load(directory)["tx"])

    def test_refund_records_intent_before_broadcast_and_never_retries(self):
        directory, st = self.ordered()

        def lost():
            self.assertIn("intent_at", canary.load(directory)["tx"]["refund"])
            raise canary.CanaryError("network lost")

        chain = CanaryChain(st, state=1, now=5_000, on_send=lost)
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError):
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertEqual(len(chain.sent), 1)
        chain = CanaryChain(st, state=1, now=5_000)
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertIn("already attempted", str(ctx.exception))
        self.assertEqual(chain.sent, [])

    def test_refund_executes_and_checks_the_result(self):
        directory, st = self.ordered(chain_id=31337, token=SHOWCASE)
        chain = CanaryChain(st, state=2, now=20_001)
        with patch.object(canary, "run", chain):
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        saved = canary.load(directory)
        self.assertEqual(saved["tx"]["refund"]["hash"], "0x" + "4" * 64)
        self.assertEqual(saved["tx"]["refund"]["block"], "0x10")
        self.assertTrue(saved["checks"]["refunded"])
        self.assertEqual(len(chain.sent), 1)
        self.assertEqual(chain.sent[0][2:5], [ESCROW, "refund(bytes32)", TASK])
        self.assertTrue((pathlib.Path(directory) / "refund.json").is_file())

    def test_refund_by_buyer_on_gas_token_allows_the_fee(self):
        fee_units = 100_000 * 20 * 10**9 // 10**12  # gasUsed * effectiveGasPrice in 18 decimals, as 6-decimal units
        cases = (("buyer", 2_000_000 - fee_units, True), ("buyer", 2_000_000 - fee_units - 1, True),
                 ("buyer", 2_000_000 - fee_units + 1, True), ("buyer", 2_000_000 - fee_units - 2, False),
                 ("buyer", 2_000_000 - fee_units + 2, False), ("relayer", 2_000_000, True))
        for role, gain, passes in cases:
            directory, st = self.ordered(name=f"{role}-{gain}")
            chain = CanaryChain(st, state=1, now=10_001, buyer_gain=gain)
            with self.subTest(role=role, gain=gain), patch.object(canary, "run", chain):
                if passes:
                    canary.cmd_refund(spend_args(directory, role=role, **self.go(st)))
                    self.assertTrue(canary.load(directory)["checks"]["refunded"])
                else:
                    with self.assertRaises(canary.CanaryError) as ctx:
                        canary.cmd_refund(spend_args(directory, role=role, **self.go(st)))
                    self.assertIn("buyer balance", str(ctx.exception))
                    self.assertNotIn("checks", canary.load(directory))

    def test_refund_by_buyer_on_a_non_gas_token_needs_the_full_amount(self):
        directory, st = self.ordered(chain_id=31337, token=SHOWCASE)
        chain = CanaryChain(st, state=1, now=5_000, buyer_gain=2_000_000 - 2_000)
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertIn("buyer balance", str(ctx.exception))

    def test_refund_post_check_fails_without_the_event(self):
        wrong = ([], [{"address": ESCROW, "topics": [REFUNDED_TOPIC, "0x" + "1" * 64], "data": "0x"}],
                 [{"address": A, "topics": [REFUNDED_TOPIC, TASK], "data": "0x"}],
                 [{"address": ESCROW, "topics": [REFUNDED_TOPIC, TASK], "data": "0x"}] * 2)
        for n, logs in enumerate(wrong):
            directory, st = self.ordered(chain_id=31337, token=SHOWCASE, name=f"logs-{n}")
            chain = CanaryChain(st, state=2, now=20_001, logs=logs)
            with self.subTest(logs=logs), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError) as ctx:
                canary.cmd_refund(spend_args(directory, **self.go(st)))
            self.assertIn("Refunded event", str(ctx.exception))
            saved = canary.load(directory)
            self.assertEqual(saved["tx"]["refund"]["hash"], "0x" + "4" * 64)
            self.assertNotIn("refunded", saved.get("checks", {}))

    def test_refund_post_check_fails_when_the_escrow_or_state_is_wrong(self):
        cases = (({"escrow_drop": 1_999_999}, "escrow balance"), ({"state_after": 2}, "the task state is not Refunded"))
        for change, message in cases:
            directory, st = self.ordered(chain_id=31337, token=SHOWCASE, name=next(iter(change)))
            chain = CanaryChain(st, state=2, now=20_001, **change)
            with self.subTest(change=change), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError) as ctx:
                canary.cmd_refund(spend_args(directory, **self.go(st)))
            self.assertIn(message, str(ctx.exception))
            self.assertNotIn("checks", canary.load(directory))

    def test_refund_post_check_refuses_a_receipt_without_gas_fields(self):
        directory, st = self.ordered()
        chain = CanaryChain(st, state=1, now=10_001)
        send = chain.send
        chain.send = lambda args: json.dumps({k: v for k, v in json.loads(send(args)).items() if k != "gasUsed"})
        with patch.object(canary, "run", chain), self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertIn("gasUsed", str(ctx.exception))
        self.assertEqual(canary.load(directory)["tx"]["refund"]["hash"], "0x" + "4" * 64)

    def test_refund_run_never_shows_the_password_path(self):
        directory, st = self.ordered(chain_id=31337, token=SHOWCASE)
        out = io.StringIO()
        chain = CanaryChain(st, state=2, now=20_001)
        with patch.dict(os.environ, {"ETH_PASSWORD": PASSWORD_FILE}), patch.object(canary, "run", chain), \
                contextlib.redirect_stdout(out):
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertIn("post-checks passed", out.getvalue())
        for text in (out.getvalue(), *(p.read_text() for p in pathlib.Path(directory).iterdir())):
            self.assertNotIn(PASSWORD_FILE, text)

    def test_refund_refuses_another_buyer_or_amount_on_chain(self):
        directory, st = self.ordered()
        for change in ({"buyer_onchain": C}, {"amount_onchain": 1_000_000}):
            chain = CanaryChain(st, state=1, now=10_001, **change)
            with self.subTest(change=change), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError) as ctx:
                canary.cmd_refund(spend_args(directory, **self.go(st)))
            self.assertIn("another buyer or amount", str(ctx.exception))
            self.assertEqual((chain.simulated, chain.sent), ([], []))
            self.assertNotIn("refund", canary.load(directory)["tx"])

    def test_refund_hash_is_saved_when_the_receipt_file_cannot_be_written(self):
        directory, st = self.ordered(chain_id=31337, token=SHOWCASE)
        (pathlib.Path(directory) / "refund.json").write_text("{}")  # write_new refuses to overwrite it
        chain = CanaryChain(st, state=2, now=20_001)
        with patch.object(canary, "run", chain), self.assertRaises(FileExistsError):
            canary.cmd_refund(spend_args(directory, **self.go(st)))
        self.assertEqual(canary.load(directory)["tx"]["refund"]["hash"], "0x" + "4" * 64)

    def test_refund_refuses_states_that_cannot_refund(self):
        cases = ((0, 20_001, "buyer"), (3, 20_001, "buyer"), (4, 20_001, "buyer"),
                 (2, 20_000, "buyer"), (2, 15_000, "relayer"),
                 (1, 10_000, "seller"), (1, 5_000, "relayer"))
        directory, st = self.ordered()
        for state, now, role in cases:
            chain = CanaryChain(st, state=state, now=now)
            with self.subTest(state=state, now=now, role=role), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError):
                canary.cmd_refund(spend_args(directory, role=role, **self.go(st)))
            self.assertEqual((chain.simulated, chain.sent), ([], []))
            self.assertNotIn("refund", canary.load(directory)["tx"])
        chain = CanaryChain(st, state=1, now=10_001)
        with patch.object(canary, "run", chain):
            canary.cmd_refund(spend_args(directory, role="relayer"))
        self.assertEqual(chain.simulated, [("refund(bytes32)", D)])

    def test_refund_needs_a_confirmed_offer(self):
        directory, st = self.ordered()
        for offer in (None, {"intent_at": "t"}):
            if offer is None:
                st["tx"].pop("offer")
            else:
                st["tx"]["offer"] = offer
            canary.save(directory, st)
            with self.subTest(offer=offer), patch.object(canary, "run") as run, \
                    self.assertRaises(canary.CanaryError) as ctx:
                canary.cmd_refund(spend_args(directory, **self.go(st)))
            self.assertIn("offer", str(ctx.exception))
            run.assert_not_called()
            self.assertNotIn("refund", canary.load(directory)["tx"])


DEPLOYMENT = {"transactions": [
    {"transactionType": "CREATE", "contractName": "RiscZeroGroth16Verifier", "hash": "0x" + "c" * 64},
    {"transactionType": "CREATE", "contractName": "SolverBountyEscrow", "hash": "0x" + "d" * 64}]}


class ReuseTests(TempCase):
    """init --reuse-deployment starts a new run against contracts that an earlier run deployed."""

    def setUp(self):
        super().setUp()
        self.old = pathlib.Path(self.init(name="old"))
        st = canary.load(self.old)
        st.update(escrow=ESCROW, verifier=VERIFIER, image_id=IMAGE_ID, code_hashes=dict(CODE_HASHES),
                  image_builder=f"{canary.BUILDER_IMAGE}:{canary.DEFAULT_BUILDER_TAG}")
        st["tx"] = {"deploy": {"intent_at": "t", "hash": "see deployment.json"},
                    "offer": {"intent_at": "t", "hash": "0x" + "3" * 64},
                    "refund": {"intent_at": "t", "hash": "0x" + "4" * 64}}
        canary.save(self.old, st)
        (self.old / "deployment.json").write_text(json.dumps(DEPLOYMENT, indent=1))
        self.old_st = st

    def snapshot(self, directory):
        return {path.name: path.read_bytes() for path in sorted(pathlib.Path(directory).iterdir())}

    def reuse(self, name="new", chain=None, **change):
        chain = chain or CanaryChain(self.old_st)
        with patch.object(canary, "run", chain):
            return self.init(name=name, reuse_deployment=str(self.old), **change)

    def test_reuse_copies_deployment_and_starts_with_empty_tx(self):
        before = self.snapshot(self.old)
        new = pathlib.Path(self.reuse())
        st = canary.load(new)
        for key in ("escrow", "verifier", "image_id", "code_hashes", "image_builder"):
            self.assertEqual(st[key], self.old_st[key], key)
        self.assertEqual((st["reused_from"], st["tx"]), ("old", {}))
        self.assertEqual((new / "deployment.json").read_bytes(), (self.old / "deployment.json").read_bytes())
        self.assertEqual(self.snapshot(self.old), before)

    def test_reuse_runs_the_read_only_probe_and_keeps_its_report(self):
        for chain_id, allow in ((5042, []), (31337, ["--allow-chain", "31337"])):
            if chain_id != 5042:
                self.old_st.update(chain_id=chain_id, roles={r: f"unlocked:{a}" for r, a in
                                                             zip(canary.ROLES, (A, B, C, D), strict=True)})
                canary.save(self.old, self.old_st)
            chain = CanaryChain(self.old_st)
            with self.subTest(chain_id=chain_id):
                signers = {} if chain_id == 5042 else self.old_st["roles"]
                new = pathlib.Path(self.reuse(name=f"new-{chain_id}", chain=chain, chain_id=chain_id, **signers))
                (args, manifest), = chain.probes
                self.assertEqual(args[2], "after")
                self.assertEqual([a for a in args if a == "--allow-chain"], allow[:1])
                if allow:
                    self.assertEqual(args[-2:], allow)
                self.assertEqual(manifest, {"escrow": ESCROW, "verifier": VERIFIER, "image_id": IMAGE_ID,
                                            "code_hashes": CODE_HASHES, "chain_id": chain_id,
                                            "token": canary.ARC_USDC})
                self.assertEqual(json.loads((new / "reuse-probe.json").read_text())["verdict"], "ok")

    def test_reuse_refuses_when_the_probe_fails(self):
        for problem in ("an order is already funded (totalReserved != 0); the probe refuses to proceed",
                        "SolverBountyEscrow code on chain differs from the local build outside the immutable slots"):
            chain = CanaryChain(self.old_st, probe_problems=[problem])
            with self.subTest(problem=problem), self.assertRaises(canary.CanaryError) as ctx:
                self.reuse(chain=chain)
            self.assertIn("read-only probe", str(ctx.exception))
            self.assertIn(problem, str(ctx.exception))
            self.assertFalse((self.root / "new").exists())

    def test_reuse_refuses_another_verifier_on_the_escrow(self):
        with self.assertRaises(canary.CanaryError) as ctx:
            self.reuse(chain=CanaryChain(self.old_st, verifier=SHOWCASE))
        self.assertIn("verifier", str(ctx.exception))
        self.assertFalse((self.root / "new").exists())

    def test_reused_run_manifest_keeps_deploy_hashes(self):
        new = pathlib.Path(self.reuse())
        with patch.object(canary.subprocess, "run", side_effect=fake_git()):
            manifest = canary.build_manifest(new, canary.load(new), None)
        self.assertEqual(manifest["transactions"], {"deploy": "0x" + "d" * 64, "deploy_verifier": "0x" + "c" * 64})

    def test_reuse_refuses_code_hash_mismatch(self):
        for name in ("escrow", "verifier"):
            chain = CanaryChain(self.old_st, code_hashes=dict(CODE_HASHES, **{name: "0x" + "f" * 64}))
            with self.subTest(name=name), self.assertRaises(canary.CanaryError) as ctx:
                self.reuse(name=f"new-{name}", chain=chain)
            self.assertIn(f"{name} code", str(ctx.exception))
            self.assertFalse((self.root / f"new-{name}").exists())

    def test_reuse_compares_hex_case_insensitively(self):
        chain = CanaryChain(self.old_st, code_hashes={k: v.upper().replace("0X", "0x") for k, v in CODE_HASHES.items()},
                            image_id=IMAGE_ID.upper().replace("0X", "0x"))
        self.assertTrue(pathlib.Path(self.reuse(chain=chain)).is_dir())

    def test_reuse_refuses_image_id_mismatch(self):
        chain = CanaryChain(self.old_st, image_id="0x" + "cd" * 32)
        with self.assertRaises(canary.CanaryError) as ctx:
            self.reuse(chain=chain)
        self.assertIn("image ID", str(ctx.exception))
        self.assertFalse((self.root / "new").exists())

    def test_reuse_refuses_an_undeployed_or_mismatched_run(self):
        undeployed = pathlib.Path(self.init(name="undeployed"))
        empty = self.root / "empty"
        empty.mkdir()
        bad_hashes = pathlib.Path(self.init(name="bad-hashes"))
        canary.save(bad_hashes, dict(self.old_st, code_hashes="0x" + "a1" * 32))
        local = {r: f"unlocked:{a}" for r, a in zip(canary.ROLES, (A, B, C, D), strict=True)}
        cases = (("undeployed", {"reuse_deployment": str(undeployed)}, None, "holds no complete deployment"),
                 ("bad-hashes", {"reuse_deployment": str(bad_hashes)}, None, "holds no complete deployment"),
                 ("missing", {"reuse_deployment": str(self.root / "nothing")}, None, "No state.json"),
                 ("empty", {"reuse_deployment": str(empty)}, None, "No state.json"),
                 ("itself", {"reuse_deployment": str(self.root / "new-itself")}, None, "No state.json"),
                 ("chain", dict(chain_id=31337, **local), None, "was deployed for chain 5042"),
                 ("token", {"token": SHOWCASE}, None, "was deployed for chain 5042"),
                 ("rpc", {}, CanaryChain(self.old_st, chain_id=1), "RPC reports chain 1"),
                 ("onchain-token", {}, CanaryChain(self.old_st, token=SHOWCASE), "different token"))
        for name, change, chain, message in cases:
            chain = chain or CanaryChain(self.old_st)
            change.setdefault("reuse_deployment", str(self.old))
            with self.subTest(case=name), patch.object(canary, "run", chain), \
                    self.assertRaises(canary.CanaryError) as ctx:
                self.init(name=f"new-{name}", **change)
            self.assertIn(message, str(ctx.exception))
            self.assertFalse((self.root / f"new-{name}").exists())
            self.assertEqual((chain.sent, chain.probes), ([], []))

    def test_deploy_refuses_in_a_reused_directory(self):
        new = self.reuse()
        with patch.object(canary, "connect"), patch.object(canary, "run") as run, \
                self.assertRaises(canary.CanaryError) as ctx:
            canary.cmd_deploy(argparse.Namespace(dir=new, allow_local_image_id=True, execute=False,
                                                 confirm_chain=None, confirm_amount=None))
        self.assertIn("Already deployed", str(ctx.exception))
        run.assert_not_called()


class EnvTests(unittest.TestCase):
    """The child environment: the caller's thread count, and ETH_PASSWORD only in env, never in argv or errors."""

    OK = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")

    def child_env(self, environ):
        with patch.dict(os.environ, environ):
            if "RAYON_NUM_THREADS" not in environ:
                os.environ.pop("RAYON_NUM_THREADS", None)  # patch.dict restores it on exit
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=self.OK) as run:
                module.run("cargo", "build")
        return run.call_args.kwargs["env"]

    def test_rayon_threads_default_to_four(self):
        self.assertEqual(self.child_env({})["RAYON_NUM_THREADS"], "4")

    def test_empty_rayon_threads_fall_back_to_four(self):
        self.assertEqual(self.child_env({"RAYON_NUM_THREADS": ""})["RAYON_NUM_THREADS"], "4")

    def test_rayon_threads_come_from_the_environment(self):
        env = self.child_env({"RAYON_NUM_THREADS": "2"})
        self.assertEqual(env["RAYON_NUM_THREADS"], "2")
        self.assertEqual((env["RISC0_BUILD_LOCKED"], env["DOCKER_DEFAULT_PLATFORM"]), ("1", "linux/amd64"))

    def test_eth_password_path_is_forwarded_in_env_only(self):
        environ = {"ETH_PASSWORD": PASSWORD_FILE, canary.RPC_ENV: "https://rpc.example/KEY"}
        with patch.dict(os.environ, environ):
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=self.OK) as run:
                module.run("cast", "send", ESCROW, "refund(bytes32)", TASK, "--account", "buyer", "--json")
                module.run("forge", "script", "script/DeploySolver.s.sol:DeploySolver", "--account", "deployer")
        self.assertEqual(run.call_count, 2)
        for child in run.call_args_list:
            self.assertEqual(child.kwargs["env"]["ETH_PASSWORD"], PASSWORD_FILE)
            self.assertNotIn(PASSWORD_FILE, " ".join(child.args[0]))

    def test_eth_password_path_does_not_reach_other_children(self):
        with patch.dict(os.environ, {"ETH_PASSWORD": PASSWORD_FILE}):
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=self.OK) as run:
                module.run("cargo", "build")
        self.assertNotIn("ETH_PASSWORD", run.call_args.kwargs["env"])

    def test_eth_password_path_does_not_reach_unsigned_foundry_calls(self):
        # forge 1.8 rejects ETH_PASSWORD without --account or --keystore, as in the deploy dry run.
        with patch.dict(os.environ, {"ETH_PASSWORD": PASSWORD_FILE, canary.RPC_ENV: "https://rpc.example/KEY"}):
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=self.OK) as run:
                module.run("forge", "script", "script/DeploySolver.s.sol:DeploySolver", "--sender", ESCROW)
                module.run("cast", "chain-id")
        for child in run.call_args_list:
            self.assertNotIn("ETH_PASSWORD", child.kwargs["env"])
            self.assertEqual(child.kwargs["env"]["ETH_RPC_URL"], "https://rpc.example/KEY")
            self.assertEqual(child.kwargs["env"]["FOUNDRY_ETH_RPC_URL"], "https://rpc.example/KEY")

    def test_eth_password_path_is_redacted_from_errors(self):
        rpc = "https://rpc.example/KEY123"
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=f"cannot read {PASSWORD_FILE} via {rpc}")
        with patch.dict(os.environ, {"ETH_PASSWORD": PASSWORD_FILE, canary.RPC_ENV: rpc}):
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=failed), \
                    self.assertRaises(module.CanaryError) as ctx:
                module.run("cast", "send", ESCROW, "refund(bytes32)", TASK)
        text = str(ctx.exception)
        for leaked in (PASSWORD_FILE, "KEY123", "rpc.example"):
            self.assertNotIn(leaked, text)
        self.assertIn("<password-file>", text)
        self.assertIn("<rpc>", text)

    def test_empty_eth_password_redacts_nothing(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="plain failure")
        with patch.dict(os.environ, {"ETH_PASSWORD": "", canary.RPC_ENV: "https://rpc.example/K"}):
            module = fresh_canary()
            with patch.object(module.subprocess, "run", return_value=failed), \
                    self.assertRaises(module.CanaryError) as ctx:
                module.run("cast", "chain-id")
        self.assertIn("plain failure", str(ctx.exception))
        self.assertNotIn("<password-file>", str(ctx.exception))


class ParserTests(unittest.TestCase):
    """The CLI surface pinned by lead decision D18. The rehearse role and the pinned-checkout grep depend on it."""

    INIT = ("init", "--dir", "new", "--amount", "2000000", "--max-cost", "14", "--instance", "i.json",
            "--deployer", "a", "--buyer", "b", "--seller", "c", "--relayer", "d")

    def parse(self, *argv):
        return canary.parser().parse_args(list(argv))

    def test_refund_defaults_to_a_dry_run_by_the_buyer(self):
        args = self.parse("refund", "--dir", "run")
        self.assertEqual((args.handler, args.dir, args.role, args.execute, args.confirm_chain, args.confirm_amount),
                         (canary.cmd_refund, "run", "buyer", False, None, None))

    def test_refund_takes_the_spending_flags_and_a_role(self):
        args = self.parse("refund", "--dir", "run", "--role", "relayer", "--execute", "--confirm-chain", "5042",
                          "--confirm-amount", "2000000")
        self.assertEqual((args.role, args.execute, args.confirm_chain, args.confirm_amount),
                         ("relayer", True, 5042, 2_000_000))

    def test_refund_role_choices_are_buyer_seller_relayer(self):
        for role in ("buyer", "seller", "relayer"):
            self.assertEqual(self.parse("refund", "--dir", "run", "--role", role).role, role)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse("refund", "--dir", "run", "--role", "deployer")

    def test_init_takes_reuse_deployment(self):
        self.assertIsNone(self.parse(*self.INIT).reuse_deployment)
        args = self.parse(*self.INIT, "--reuse-deployment", "mainnet/20261015T120000Z")
        self.assertEqual((args.handler, args.reuse_deployment), (canary.cmd_init, "mainnet/20261015T120000Z"))


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


COMMIT = "a" * 40
TX = "0x" + "e" * 64


def fake_git(porcelain=""):
    """A subprocess.run replacement that answers only the two git commands the evidence step uses."""
    def fake(cmd, **_):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=COMMIT + "\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=porcelain, stderr="")
        raise AssertionError(f"unexpected command {cmd}")
    return fake


class EvidenceTests(TempCase):
    """The evidence step writes manifest.json beside result.json and enforces the local/mainnet folder rule."""

    def setUp(self):
        super().setUp()
        self.canary = self.root / "canary"
        self.patch = patch.object(canary, "CANARY_ROOT", self.canary)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def deployed(self, folder="local", chain_id=31337, name="20261014T120000Z", builder=None):
        kind = "ledger" if chain_id in canary.MAINNET_IDS else "unlocked"
        signers = {role: f"{kind}:{addr}" for role, addr in zip(canary.ROLES, (A, B, C, D), strict=True)}
        directory = self.init(name=f"canary/{folder}/{name}", chain_id=chain_id, **signers)
        st = canary.load(directory)
        st.update(escrow=A, verifier=B, image_id="0x" + "ab" * 32,
                  image_builder=builder or f"{canary.BUILDER_IMAGE}:{canary.DEFAULT_BUILDER_TAG}",
                  code_hashes={"escrow": "0x" + "1" * 64, "verifier": "0x" + "2" * 64})
        st["tx"] = {"deploy": {"intent_at": "t", "hash": "see deployment.json"},
                    "offer": {"intent_at": "t", "hash": TX}}
        canary.save(directory, st)
        (pathlib.Path(directory) / "deployment.json").write_text(json.dumps({"transactions": [
            {"transactionType": "CREATE", "contractName": "RiscZeroGroth16Verifier", "hash": "0x" + "c" * 64},
            {"transactionType": "CREATE", "contractName": "SolverBountyEscrow", "hash": "0x" + "d" * 64}]}))
        return pathlib.Path(directory)

    def evidence(self, directory, digest=None, porcelain=""):
        with patch.object(canary.subprocess, "run", side_effect=fake_git(porcelain)):
            canary.cmd_evidence(argparse.Namespace(dir=str(directory), worker_digest=digest))

    def test_evidence_writes_manifest_with_required_keys(self):
        directory = self.deployed()
        self.evidence(directory, porcelain=" M scripts/solver-canary.py\n")
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(set(canary.MANIFEST_KEYS) - set(manifest), set())
        self.assertEqual((manifest["source_commit"], manifest["source_dirty"], manifest["chain_id"]),
                         (COMMIT, True, 31337))
        self.assertEqual(manifest["compiler"], {"solc": "0.8.28", "evm_version": "cancun", "optimizer_runs": 200})
        self.assertEqual(manifest["transactions"],
                         {"deploy": "0x" + "d" * 64, "deploy_verifier": "0x" + "c" * 64, "offer": TX})
        self.assertNotIn("worker_image", manifest)
        self.assertTrue((directory / "result.json").is_file())
        canary.check_manifest(manifest)

    def test_clean_tree_is_not_dirty(self):
        directory = self.deployed(name="20261014T120100Z")
        self.evidence(directory)
        self.assertFalse(json.loads((directory / "manifest.json").read_text())["source_dirty"])

    def test_evidence_refuses_wrong_folder_for_chain(self):
        cases = (("mainnet", 31337, "20261014T120000Z"), ("local", 5042, "20261014T120000Z"),
                 ("local", 1, "20261014T120001Z"), ("local", 31337, "nested/20261014T120000Z"))
        for folder, chain_id, name in cases:
            with self.subTest(folder=folder, chain_id=chain_id, name=name):
                directory = self.deployed(folder=folder, chain_id=chain_id, name=name)
                with self.assertRaises(canary.CanaryError):
                    self.evidence(directory)
                self.assertFalse((directory / "manifest.json").exists())
                self.assertFalse((directory / "result.json").exists())

    def test_mainnet_manifest_needs_a_docker_builder(self):
        directory = self.deployed(folder="mainnet", chain_id=5042, builder="local")
        with self.assertRaises(canary.CanaryError) as ctx:
            self.evidence(directory)
        self.assertIn("Docker builder", str(ctx.exception))

    def test_worker_digest_pattern_is_enforced(self):
        directory = self.deployed()
        for bad in ("sha256:abc", "sha256:" + "A" * 64, "md5:" + "a" * 64, "a" * 64):
            with self.subTest(bad=bad), self.assertRaises(canary.CanaryError):
                self.evidence(directory, digest=bad)
        self.assertFalse((directory / "manifest.json").exists())
        good = "sha256:" + "f" * 64
        self.evidence(directory, digest=good)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["worker_image"], {"repository": canary.WORKER_REPOSITORY, "digest": good})

    def test_manifest_never_contains_the_rpc_url(self):
        directory = self.deployed()
        with patch.dict(os.environ, {canary.RPC_ENV: "https://rpc.example/KEY123"}):
            self.evidence(directory)
        for name in ("manifest.json", "result.json"):
            self.assertNotIn("KEY123", (directory / name).read_text())
            self.assertNotIn("rpc.example", (directory / name).read_text())

    def test_evidence_files_are_never_overwritten(self):
        directory = self.deployed()
        self.evidence(directory)
        before = {name: (directory / name).read_text() for name in ("manifest.json", "result.json")}
        with self.assertRaises(canary.CanaryError) as ctx:
            self.evidence(directory, porcelain="?? other\n")
        self.assertIn("already exists", str(ctx.exception))
        for name, text in before.items():
            self.assertEqual((directory / name).read_text(), text)
        (directory / "manifest.json").unlink()
        with self.assertRaises(canary.CanaryError):
            self.evidence(directory)
        self.assertFalse((directory / "manifest.json").exists())

    def test_evidence_needs_a_deployment(self):
        signers = {role: f"unlocked:{addr}" for role, addr in zip(canary.ROLES, (A, B, C, D), strict=True)}
        directory = self.init(name="canary/local/20261014T130000Z", chain_id=31337, **signers)
        with self.assertRaises(canary.CanaryError) as ctx:
            self.evidence(directory)
        self.assertIn("Deploy first", str(ctx.exception))

    def test_check_manifest_rejects_bad_values(self):
        directory = self.deployed()
        self.evidence(directory)
        good = json.loads((directory / "manifest.json").read_text())
        for change in ({"source_commit": "abc"}, {"chain_id": 5042002}, {"image_id": "0x12"},
                       {"compiler": {"solc": "0.8.27", "evm_version": "cancun", "optimizer_runs": 200}},
                       {"transactions": {"bogus": TX}}, {"code_hashes": {"escrow": "0x1", "verifier": "0x2"}},
                       {"created_at": "yesterday"}, {"source_dirty": "no"}):
            with self.subTest(change=change), self.assertRaises(canary.CanaryError):
                canary.check_manifest(dict(good, **change))

    def test_mainnet_note_carries_the_scripted_canary_statement(self):
        existing = ("The checker proves a modelled cost, not a cloud bill.", "Settlement does not start any cloud run.")
        mainnet = self.deployed(folder="mainnet", chain_id=5042)
        local = self.deployed(folder="local", chain_id=31337)
        for directory in (mainnet, local):
            self.evidence(directory)
        notes = {d: json.loads((d / "result.json").read_text())["note"] for d in (mainnet, local)}
        for fragment in ("Scripted canary", "four roles", "scripts/solver-agent.py", "next feature"):
            self.assertIn(fragment, notes[mainnet])
            self.assertNotIn(fragment, notes[local])
        for directory in (mainnet, local):
            for sentence in existing:
                self.assertIn(sentence, notes[directory])

    def test_result_records_reused_from(self):
        directory = self.deployed()
        st = canary.load(directory)
        st["reused_from"] = "20261014T110000Z"
        canary.save(directory, st)
        self.evidence(directory)
        self.assertEqual(json.loads((directory / "result.json").read_text())["reused_from"], "20261014T110000Z")

    def test_refund_hash_passes_the_manifest_check(self):
        directory = self.deployed()
        st = canary.load(directory)
        st["tx"]["refund"] = {"intent_at": "t", "hash": "0x" + "4" * 64, "block": "0x10"}
        canary.save(directory, st)
        self.evidence(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["transactions"]["refund"], "0x" + "4" * 64)
        canary.check_manifest(manifest)
        schema = json.loads((pathlib.Path(canary.ROOT) / "solver-bounty/canary/manifest.schema.json").read_text())
        self.assertEqual(schema["properties"]["transactions"]["properties"]["refund"], {"$ref": "#/$defs/hash32"})


if __name__ == "__main__":
    unittest.main()
