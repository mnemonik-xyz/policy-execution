"""Fail-closed checks for release identity and symbolic evidence admission."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("proof_release", Path(__file__).with_name("proof-release.py"))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseGateTests(unittest.TestCase):
    def test_settlement_requires_real_proof_and_complete_readback(self):
        info = {"imageId": "0x" + "01" * 32}
        result = dict(info, proofOnly=True, realProof=True, replayRejected=True,
                      wrongImageRejected=True, journalTamperingRejected=True,
                      deploymentCodeMatched=True, tamperedJournalWordsRejected=15)
        release.validate_settlement(result, info)
        for key in result:
            for value in (None, False, "true"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    release.validate_settlement(dict(result, **{key: value}), info)

    def report(self):
        return {"exitcode": 0, "test_results": {
            "test/ProofInvoiceEscrow.t.sol:ProofInvoiceEscrowTest": [
                dict(name=name + "()", exitcode=0, num_bounded_loops=0, num_models=0)
                for name in release.PROPERTIES]}}

    def test_complete_symbolic_report(self):
        release.validate_symbolic(self.report())

    def test_missing_failed_bounded_or_counterexample_rejected(self):
        for field in ("exitcode", "num_bounded_loops", "num_models"):
            report = self.report()
            report["test_results"]["test/ProofInvoiceEscrow.t.sol:ProofInvoiceEscrowTest"][0][field] = 1
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.validate_symbolic(report)
        report = self.report()
        report["test_results"]["test/ProofInvoiceEscrow.t.sol:ProofInvoiceEscrowTest"].pop()
        with self.assertRaises(ValueError):
            release.validate_symbolic(report)

    def test_duplicate_report_cannot_replace_missing_property(self):
        report = self.report()
        rows = report["test_results"]["test/ProofInvoiceEscrow.t.sol:ProofInvoiceEscrowTest"]
        rows[-1] = copy.deepcopy(rows[0])
        with self.assertRaises(ValueError):
            release.validate_symbolic(report)

    def test_wrong_guest_schema_and_zero_id_rejected(self):
        info = dict(guest="warrant-invoice-guest", journalSchema="warrant/invoice-journal/v1",
                    journalBytes=480, imageId="0x" + "01" * 32)
        release.validate_identity(info)
        for key, value in (("guest", "warrant-guest"), ("journalBytes", 384),
                           ("journalSchema", "unknown"), ("imageId", "0x" + "00" * 32)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_identity(dict(info, **{key: value}))


if __name__ == "__main__":
    unittest.main()
