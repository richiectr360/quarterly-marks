"""Hypothetical requests remain explicit, reproducible and distinct from actual marks."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quarterly_marks import audit
from quarterly_marks.domain import AuditError, Quarter
from quarterly_marks.inputs import load_dataset
from quarterly_marks.valuation import build_report


def company(report, name):
    return next(row for row in report["companies"] if row["company"] == name)


def reseal(receipt):
    """An attacker can recompute hashes; replay must still verify the calculation."""
    unsigned = {key: value for key, value in receipt.items() if key != "integrity_sha256"}
    receipt["integrity_sha256"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")).hexdigest()
    return receipt


class ScenarioAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(ROOT / "data")
        cls.quarter = Quarter.parse("2026Q3")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def receipt(self, overrides=None):
        report = build_report(self.dataset, self.quarter, overrides=overrides)
        return audit.create_receipt(self.dataset, report)

    def raw_receipt(self, receipt):
        path = self.directory / "supplied.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return path

    def test_v3_ordinary_receipt_has_explicit_empty_scenario(self):
        receipt = self.receipt()
        self.assertEqual(receipt["schema_version"], 3)
        self.assertEqual(receipt["engine"]["version"], "3.0.0")
        self.assertIn("quarterly_marks/scenarios.py", receipt["engine"]["code_sha256"])
        self.assertEqual(receipt["request"], {
            "quarter": "2026Q3", "as_of": None, "hypothetical": False, "overrides": [], "include_attribution": False,
        })
        self.assertFalse(receipt["report"]["hypothetical"])
        self.assertEqual(receipt["report"]["overrides"], [])
        self.assertEqual(audit.load_receipt(self.raw_receipt(receipt)), receipt)

    def test_override_receipt_preserves_original_rows_and_normalizes_request(self):
        overrides = [{"company": "Pinewell", "field": "arr", "value": "13000000.00"}]
        original_argument = copy.deepcopy(overrides)
        original_sources = dict(self.dataset.sources)
        receipt = self.receipt(overrides)
        expected = [{"company": "Pinewell", "field": "arr", "value": "13000000"}]
        self.assertEqual(overrides, original_argument)
        self.assertEqual(dict(self.dataset.sources), original_sources)
        self.assertTrue(receipt["request"]["hypothetical"])
        self.assertTrue(receipt["report"]["hypothetical"])
        self.assertEqual(receipt["request"]["overrides"], expected)
        self.assertEqual(receipt["report"]["overrides"], expected)
        row = company(receipt["report"], "Pinewell")
        self.assertEqual(row["arr"], "13000000")
        self.assertEqual(row["rounded_fund_position_value"], "9166667")
        self.assertEqual(row["selected_inputs"]["arr"]["value"], "13000")
        self.assertEqual(row["selected_inputs"]["arr"]["source"]["raw"]["arr"], "13000")
        self.assertEqual(receipt["sources"]["kpis.csv"]["text"], original_sources["kpis.csv"])

    def test_replay_reapplies_captured_overrides_and_preserves_lineage(self):
        for expression, expected in (
                ({"company": "Pinewell", "field": "arr", "value": "13000000"}, "9166667"),
                ({"company": "Kestrel Labs", "field": "net_cash", "value": "-5000000"}, "20833333")):
            with self.subTest(override=expression):
                original = self.receipt([expression])
                path = audit.write_receipt(original, self.directory / "receipts")
                with mock.patch("quarterly_marks.inputs.load_dataset", side_effect=AssertionError("live inputs read")):
                    saved, reproduced = audit.replay_receipt(path)
                self.assertEqual(reproduced, original["report"])
                self.assertEqual(company(reproduced, expression["company"])["rounded_fund_position_value"], expected)
                replay = audit.create_receipt(self.dataset, reproduced, operation="replay",
                                              parent_run_ids=[saved["run_id"]])
                self.assertEqual(replay["request"], original["request"])
                self.assertEqual(replay["parent_run_ids"], [original["run_id"]])
                self.assertNotEqual(replay["run_id"], original["run_id"])

    def test_override_tampering_without_resealing_is_rejected(self):
        receipt = self.receipt([{"company": "Pinewell", "field": "arr", "value": "13000000"}])
        receipt["request"]["overrides"][0]["value"] = "14000000"
        with self.assertRaisesRegex(AuditError, "integrity hash mismatch"):
            audit.load_receipt(self.raw_receipt(receipt))

    def test_resealed_request_cannot_disagree_with_report_scenario(self):
        receipt = self.receipt([{"company": "Pinewell", "field": "arr", "value": "13000000"}])
        receipt["request"]["overrides"][0]["value"] = "14000000"
        with self.assertRaises(AuditError):
            audit.load_receipt(self.raw_receipt(reseal(receipt)))

    def test_resealed_changed_scenario_cannot_reuse_an_unchanged_result(self):
        receipt = self.receipt([{"company": "Pinewell", "field": "arr", "value": "13000000"}])
        for section in ("request", "report"):
            receipt[section]["overrides"][0]["value"] = "14000000"
        path = self.raw_receipt(reseal(receipt))
        with self.assertRaises(AuditError):
            audit.replay_receipt(path)

    def test_resealed_false_actual_label_is_rejected(self):
        receipt = self.receipt([{"company": "Pinewell", "field": "arr", "value": "13000000"}])
        for section in ("request", "report"):
            receipt[section]["hypothetical"] = False
        with self.assertRaises(AuditError):
            audit.load_receipt(self.raw_receipt(reseal(receipt)))

    def test_resealed_invalid_scenario_is_rejected_even_without_engine_compatibility(self):
        valid = {"company": "Pinewell", "field": "arr", "value": "13000000"}
        cases = [
            [dict(valid, field="unrecognized")],
            [dict(valid, company="Unknown Company")],
            [dict(valid, value="NaN")],
            [dict(valid, value=13000000)],
            [dict(valid, value="-1")],
            [valid, dict(valid)],
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                receipt = self.receipt([valid])
                for section in ("request", "report"):
                    receipt[section]["overrides"] = copy.deepcopy(overrides)
                with self.assertRaises(AuditError):
                    audit.load_receipt(self.raw_receipt(reseal(receipt)), require_compatible=False)

    def test_no_op_scenarios_remain_distinct_in_saved_comparisons(self):
        ordinary = self.receipt()
        expected_marks = {row["company"]: row["rounded_fund_position_value"]
                          for row in ordinary["report"]["companies"]}
        for expression in ({"company": "Pinewell", "field": "arr", "value": "13000"},
                           {"company": "Pinewell", "field": "net_cash", "value": "0"}):
            with self.subTest(override=expression):
                hypothetical = self.receipt([expression])
                self.assertEqual({row["company"]: row["rounded_fund_position_value"]
                                  for row in hypothetical["report"]["companies"]}, expected_marks)
                self.assertEqual(hypothetical["sources"], ordinary["sources"])
                comparison = audit.compare_receipts(ordinary, hypothetical)
                self.assertTrue(comparison["has_changes"])
                self.assertFalse(comparison["first_request"]["hypothetical"])
                self.assertTrue(comparison["second_request"]["hypothetical"])
                self.assertEqual(comparison["second_request"]["overrides"], [expression])
                receipt = audit.create_comparison_receipt(ordinary, hypothetical, comparison)
                # Neither parent needs a standalone file: comparison embeds both.
                path = audit.write_receipt(receipt, self.directory / "comparisons")
                with mock.patch("quarterly_marks.valuation.build_report", side_effect=AssertionError("unexpected revaluation")):
                    saved, reproduced = audit.replay_receipt(path)
                self.assertEqual(reproduced, comparison)
                self.assertEqual(saved["parent_run_ids"], [ordinary["run_id"], hypothetical["run_id"]])
                self.assertTrue(saved["right_receipt"]["report"]["hypothetical"])


if __name__ == "__main__":
    unittest.main()
