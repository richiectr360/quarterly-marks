"""What-if values are validated, explicit and isolated from historical facts."""

from dataclasses import replace
from datetime import date
from decimal import localcontext
import unittest

from quarterly_marks.domain import MarksError
from quarterly_marks.inputs import load_dataset
from quarterly_marks.scenarios import normalize_overrides, parse_overrides
from quarterly_marks.valuation import build_report
from test_valuation import DATA, Q2, Q3, codes, company, observation, simple_dataset


class OverrideValidationTests(unittest.TestCase):
    def test_canonical_sorted_request_and_signed_cash(self):
        self.assertEqual(parse_overrides(["Z.net_cash=-1.500", "A.multiple=+2e0", "A.arr=-0"]), [
            {"company": "A", "field": "arr", "value": "0"},
            {"company": "A", "field": "multiple", "value": "2"},
            {"company": "Z", "field": "net_cash", "value": "-1.5"}])
        self.assertEqual(normalize_overrides(None), [])
        self.assertEqual(normalize_overrides([]), [])

    def test_malformed_unknown_duplicate_and_unbounded_requests_fail(self):
        bad_text = ["A.arr", ".arr=1", "A.nope=1", "A.arr=NaN", "A.arr=Infinity",
                    "A.arr=-1", "A.multiple=-1", "A.net_cash=-1e25", "A.arr=1e25",
                    "A.arr=1e-13", "A.arr=1_000", "A.arr=--1", "A\x00.arr=1"]
        for value in bad_text:
            with self.subTest(value=value), self.assertRaises(MarksError):
                parse_overrides([value])
        for value in ({}, [None], [{"company": "A", "field": "arr", "value": 1}],
                      [{"company": "A", "field": "arr", "value": "1", "extra": True}]):
            with self.subTest(value=value), self.assertRaises(MarksError):
                normalize_overrides(value)
        with self.assertRaisesRegex(MarksError, "duplicate"):
            parse_overrides(["A.arr=1", " A.arr=2"])
        with self.assertRaisesRegex(MarksError, "unknown company"):
            normalize_overrides(parse_overrides(["Missing.arr=1"]), {"A"})


class ScenarioValuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(DATA)

    def test_pinewell_scenario_preserves_source_and_prior_calculation(self):
        before = build_report(self.dataset, Q3)
        source_text = dict(self.dataset.sources)
        overrides = parse_overrides(["Pinewell.arr=13000000"])
        report = build_report(self.dataset, Q3, overrides=overrides)
        row = company(report, "Pinewell")
        self.assertTrue(report["hypothetical"])
        self.assertEqual(report["overrides"], overrides)
        self.assertEqual(row["rounded_fund_position_value"], "9166667")
        self.assertEqual(row["arr"], "13000000")
        self.assertEqual(row["selected_inputs"]["arr"]["value"], "13000")
        self.assertEqual(row["overrides"][0]["original_value"], "13000")
        self.assertEqual(row["overrides"][0]["original_input"], row["selected_inputs"]["arr"])
        self.assertEqual(row["prior_recalculation"], company(before, "Pinewell")["prior_recalculation"])
        self.assertIn("HYPOTHETICAL_OVERRIDE", codes(row))
        self.assertEqual(dict(self.dataset.sources), source_text)
        self.assertEqual(build_report(self.dataset, Q3), before)

    def test_cash_bridge_allows_signed_cash_but_rejects_negative_equity(self):
        dataset = simple_dataset()
        for cash, equity, fund in (("20", "120", "60"), ("-20", "80", "40"), ("-100", "0", "0")):
            row = build_report(dataset, Q3, overrides=parse_overrides([f"Example.net_cash={cash}"]))["companies"][0]
            self.assertEqual((row["enterprise_value"], row["equity_value"], row["rounded_fund_position_value"]),
                             ("100", equity, fund))
            self.assertEqual(row["overrides"][0]["original_value"], "0")
            self.assertIsNone(row["overrides"][0]["original_input"])
        with self.assertRaisesRegex(MarksError, "equity value cannot be negative"):
            build_report(dataset, Q3, overrides=parse_overrides(["Example.net_cash=-101"]))

    def test_overrides_fill_missing_current_inputs_with_null_original_evidence(self):
        dataset = replace(simple_dataset(), kpis=(), multiples=())
        row = build_report(dataset, Q3, overrides=parse_overrides(
            ["Example.arr=100", "Example.multiple=1"]))["companies"][0]
        self.assertEqual(row["rounded_fund_position_value"], "50")
        for field in ("arr", "multiple"):
            self.assertIsNone(row["selected_inputs"][field])
        self.assertTrue(all(item["original_input"] is None and item["original_value"] is None
                            for item in row["overrides"]))

    def test_overrides_do_not_hide_source_conflicts(self):
        dataset = simple_dataset()
        conflicting = observation("101", row=99)
        with self.assertRaisesRegex(MarksError, "conflicting versions"):
            build_report(replace(dataset, kpis=(*dataset.kpis, conflicting)), Q3,
                         overrides=parse_overrides(["Example.arr=100"]))

    def test_historical_scenario_is_hypothetical_and_preserves_published_mark(self):
        row = company(build_report(self.dataset, Q2, date(2026, 8, 14),
                      overrides=parse_overrides(["Halcyon Metrics.arr=21000000"])), "Halcyon Metrics")
        self.assertEqual(row["selected_inputs"]["published_mark"]["value"], "23684211")
        self.assertIn("PUBLISHED_MARK_DIFFERENCE", codes(row))

    def test_scenario_normalization_and_finance_ignore_ambient_precision(self):
        overrides = parse_overrides(["Halcyon Metrics.net_cash=-1000000.123456789012",
                                     "Halcyon Metrics.multiple=8.123456789012"])
        expected = build_report(self.dataset, Q3, overrides=overrides)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(build_report(self.dataset, Q3, overrides=list(reversed(overrides))), expected)
        self.assertEqual(company(expected, "Halcyon Metrics")["prior_recalculation"],
                         company(build_report(self.dataset, Q3), "Halcyon Metrics")["prior_recalculation"])


if __name__ == "__main__":
    unittest.main()
