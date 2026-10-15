"""Regression tests for the real io.net workload instance and its provenance record.

They tie solver-bounty/instance-ionet.json, solver-bounty/instance-ionet.md and the
reference seller together. They need the native checker but no network or chain.

Run from the repository root after `cargo build --locked -p warrant-solver`:
python3 scripts/test_instance_ionet.py
"""
import decimal
import importlib.util
import json
import math
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTANCE = ROOT / "solver-bounty/instance-ionet.json"
RECORD = ROOT / "solver-bounty/instance-ionet.md"
BUNDLED = ROOT / "solver-bounty/instance.json"
BINARY = ROOT / "target/debug/warrant-solver"

SPEC = importlib.util.spec_from_file_location("solver_agent", pathlib.Path(__file__).with_name("solver-agent.py"))
solver_agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(solver_agent)

VALUE_ROWS = ["estimate_total_usd", "estimate_observed_at", "cost_unit", "price_per_unit_tick",
              "deadline", "max_cost", "instance_hash"]
MACHINE_FIELDS = {"capacity", "price_per_unit_tick"}
JOB_FIELDS = {"units", "durations", "release", "deadline", "dependencies"}
SECONDS_PER_HOUR = 3600
SEGMENT_SECONDS = 30
MICRO = decimal.Decimal(1_000_000)


def native(*args):
    if not BINARY.is_file():
        raise AssertionError(f"{BINARY} is missing: run cargo build --locked -p warrant-solver")
    done = subprocess.run([str(BINARY), *map(str, args)], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"warrant-solver {args[0]} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def load_instance():
    return json.loads(INSTANCE.read_text())


def record():
    return RECORD.read_text()


def section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        raise AssertionError(f"Section '## {heading}' is missing from {RECORD.name}")
    return match.group(1)


def table(body):
    """Return the rows of every two-column Markdown table in body, in order, without header or rule."""
    rows = []
    for line in body.splitlines():
        match = re.fullmatch(r"\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|", line.strip())
        if match and not set(match.group(1)) <= set("-:") and match.group(1) not in ("Key", "Field"):
            rows.append((match.group(1), match.group(2)))
    return rows


def values():
    rows = table(section(record(), "Values"))
    keys = [key for key, _ in rows]
    if keys != VALUE_ROWS:
        raise AssertionError(f"Values rows must be exactly {VALUE_ROWS}, found {keys}")
    return dict(rows)


def excerpt():
    match = re.search(r"```json\n(.*?)\n```", section(record(), "Estimate"), re.S)
    if match is None:
        raise AssertionError("The Estimate section has no fenced json excerpt")
    return json.loads(match.group(1))


def integer(value, name):
    if not re.fullmatch(r"[0-9]+", value):
        raise AssertionError(f"Values row {name} must be a plain integer, found {value!r}")
    return int(value)


class InstanceTests(unittest.TestCase):
    def test_instance_has_only_checker_fields(self):
        instance = load_instance()
        self.assertEqual(set(instance), {"machines", "jobs"})
        self.assertTrue(instance["machines"] and instance["jobs"])
        for machine in instance["machines"]:
            self.assertEqual(set(machine), MACHINE_FIELDS)
        for job in instance["jobs"]:
            self.assertEqual(set(job), JOB_FIELDS)

    def test_native_checker_accepts_instance(self):
        self.assertRegex(native("instance-hash", INSTANCE), r"\A0x[0-9a-f]{64}\Z")

    def test_recorded_hash_matches_instance(self):
        self.assertEqual(values()["instance_hash"], native("instance-hash", INSTANCE))

    def test_reference_seller_meets_max_cost(self):
        schedule, heuristic_cost = solver_agent.solve(load_instance())
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "schedule.json"
            path.write_text(json.dumps(schedule))
            checked = json.loads(native("check", INSTANCE, path))
        self.assertEqual(checked["totalCost"], heuristic_cost)
        self.assertLessEqual(heuristic_cost, integer(values()["max_cost"], "max_cost"))

    def test_instance_is_not_bundled_synthetic(self):
        self.assertNotEqual(load_instance(), json.loads(BUNDLED.read_text()))
        self.assertNotEqual(native("instance-hash", INSTANCE), native("instance-hash", BUNDLED))


class RecordTests(unittest.TestCase):
    def test_provenance_record_is_complete(self):
        text = record()
        audio = section(text, "Audio")
        self.assertRegex(audio, r"\b[0-9a-f]{64}\b")
        self.assertRegex(section(text, "Estimate"), r"observed_at\D+[0-9]{10}\b")
        self.assertIn("caas_get_price_estimate", text)
        self.assertRegex(audio, r"https://librivox\.org/\S+")
        self.assertIn("public domain", audio.lower())
        self.assertIn("The checker proves a modelled cost, not the bill.", section(text, "Limits"))

    def test_estimate_excerpt_matches_values(self):
        estimate, rows = excerpt(), values()
        self.assertEqual(estimate["tool"], "caas_get_price_estimate")
        arguments = estimate["arguments"]
        self.assertEqual((arguments["hardware_id"], arguments["location_ids"]), ("gpu_1x_a6000", "US"))
        self.assertEqual((arguments["duration_hours"], arguments["gpus_per_container"]), (1, 1))
        total = estimate["result"]["data"]["data"]["total_cost_usdc"]
        self.assertEqual(decimal.Decimal(str(total)), decimal.Decimal(rows["estimate_total_usd"]))
        self.assertEqual(estimate["observed_at"], integer(rows["estimate_observed_at"], "estimate_observed_at"))

    def test_machine_price_follows_estimate(self):
        rows, instance = values(), load_instance()
        micro_usd = decimal.Decimal(rows["estimate_total_usd"]) * MICRO
        self.assertEqual(micro_usd, micro_usd.to_integral_value(), "estimate is not a whole micro-USD amount")
        price = integer(rows["price_per_unit_tick"], "price_per_unit_tick")
        self.assertEqual(price, int(micro_usd))
        arguments = excerpt()["arguments"]
        deadline = integer(rows["deadline"], "deadline")
        self.assertEqual(deadline, SECONDS_PER_HOUR * arguments["duration_hours"])
        self.assertEqual(integer(rows["max_cost"], "max_cost"), deadline * price)
        self.assertEqual(instance["machines"], [{"capacity": arguments["gpus_per_container"],
                                                  "price_per_unit_tick": price}])
        for job in instance["jobs"]:
            self.assertEqual((job["units"], job["release"], job["deadline"], job["dependencies"]),
                             (1, 0, deadline, []))

    def test_job_durations_follow_audio(self):
        rows = dict(table(section(record(), "Audio")))
        seconds = decimal.Decimal(rows["Duration (s)"])
        whole = int(seconds // SEGMENT_SECONDS)
        remainder = seconds - whole * SEGMENT_SECONDS
        # Full 30 s segments; the remainder is folded into the last one and rounded up.
        expected = [SEGMENT_SECONDS] * (whole - 1) + [SEGMENT_SECONDS + math.ceil(remainder)]
        self.assertEqual([job["durations"] for job in load_instance()["jobs"]], [[d] for d in expected])

    def test_cost_unit_is_named(self):
        unit = values()["cost_unit"]
        self.assertTrue(unit)
        self.assertNotEqual(unit.strip().upper(), "USD")


if __name__ == "__main__":
    unittest.main()
