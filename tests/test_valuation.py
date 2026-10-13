"""Financial and historical regression tests independent of CLI presentation."""

from __future__ import annotations

import json
import itertools
import random
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from quarterly_marks.domain import CapTable, Dataset, MarksError, Observation, Quarter, Series, SourceRow
from quarterly_marks.inputs import load_dataset, parse_sources
from quarterly_marks.selection import select_latest
from quarterly_marks.valuation import build_report


DATA = Path(__file__).resolve().parents[1] / "data"
Q2, Q3 = Quarter.parse("2026Q2"), Quarter.parse("2026Q3")
D = Decimal
F = Fraction

# Independently derived exact fractions, not values copied from allocate().
# High-value Halcyon/Orbitra classes share common value pro rata. Pinewell Q2
# pays B's 36m preference then splits the remaining 89m 2:1. Kestrel Q2 pays
# B's 50m preference then splits 54m 2:1. Q3 Orbitra is exactly $12 per share;
# Pinewell's senior B consumes 91k; Kestrel's 55m pays A/B claims in ratio 1:5.
CLASS_PAYOUT_FIXTURES = {
    ("2026Q2", "Halcyon Metrics"): {
        "Common": F(180_000_000 * 10, 19),
        "Series A": F(180_000_000 * 4, 19), "Series B": F(180_000_000 * 5, 19),
    },
    ("2026Q2", "Orbitra"): {
        "Common": F(160_000_000 * 8, 18),
        "Series A": F(160_000_000 * 6, 18), "Series B": F(160_000_000 * 4, 18),
    },
    ("2026Q2", "Pinewell"): {
        "Common": F(178_000_000, 3), "Series A": F(89_000_000, 3), "Series B": F(36_000_000),
    },
    ("2026Q2", "Kestrel Labs"): {
        "Common": F(36_000_000), "Series A": F(18_000_000), "Series B": F(50_000_000),
    },
    ("2026Q3", "Halcyon Metrics"): {
        "Common": F(168_000_000 * 10, 19),
        "Series A": F(168_000_000 * 4, 19), "Series B": F(168_000_000 * 5, 19),
    },
    ("2026Q3", "Orbitra"): {
        "Common": F(96_000_000), "Series A": F(72_000_000),
        "Series B": F(48_000_000), "Series C": F(30_000_000),
    },
    ("2026Q3", "Pinewell"): {"Common": F(0), "Series A": F(0), "Series B": F(91_000)},
    ("2026Q3", "Kestrel Labs"): {
        "Common": F(0), "Series A": F(55_000_000, 6), "Series B": F(275_000_000, 6),
    },
}


def company(report: dict, name: str) -> dict:
    return next(row for row in report["companies"] if row["company"] == name)


def codes(row: dict) -> set[str]:
    return {warning["code"] for warning in row["warnings"]}


def observation(value: str, quarter: Quarter = Q3, known: date = date(2026, 10, 1),
                *, filename: str = "kpis.csv", row: int = 2) -> Observation:
    return Observation("Example", quarter, D(value), known,
                       SourceRow(filename, row, (("value", value),)))


def simple_dataset(*, cap: CapTable | None = None, arr: str = "100", multiple: str = "1",
                   prior_arr: str | None = "100", prior_mark: str | None = "50") -> Dataset:
    current_arr = observation(arr)
    kpis = [current_arr]
    if prior_arr is not None:
        kpis.append(observation(prior_arr, Q2, date(2026, 7, 1), row=3))
    reported = (() if prior_mark is None else
                (observation(prior_mark, Q2, date(2026, 8, 14), filename="q2_marks.csv"),))
    return Dataset(tuple(kpis), (observation(multiple, filename="valuation_inputs.csv"),),
                   reported, {"Example": cap or CapTable(1, (
                       Series("A", 1, D("1"), date(2025, 1, 1), 1, 1),))}, {})


class SuppliedDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(DATA)

    def test_q2_reproduces_each_published_whole_dollar_mark(self) -> None:
        report = build_report(self.dataset, Q2, date(2026, 8, 14))
        self.assertEqual({row["company"]: row["rounded_fund_position_value"]
                          for row in report["companies"]}, {
            "Halcyon Metrics": "23684211", "Orbitra": "26666667",
            "Pinewell": "14833333", "Kestrel Labs": "25000000",
        })
        for row in report["companies"]:
            self.assertIsNone(row["prior_quarter_mark"])
            self.assertIsNone(row["mark_change_pct"])
            self.assertIn("MISSING_PRIOR_MARK", codes(row))
            self.assertIsNotNone(row["arr_growth_pct"])

    def test_q3_latest_supplied_values_and_warnings(self) -> None:
        report = build_report(self.dataset, Q3)
        self.assertEqual(report["cutoff_mode"], "latest_supplied")
        self.assertIsNone(report["as_of"])
        self.assertEqual({row["company"]: row["rounded_fund_position_value"]
                          for row in report["companies"]}, {
            "Halcyon Metrics": "22105263", "Orbitra": "36000000",
            "Pinewell": "0", "Kestrel Labs": "22916667",
        })
        pinewell = company(report, "Pinewell")
        self.assertEqual(pinewell["arr"], "13000")
        self.assertEqual(D(pinewell["class_allocations"]["Series B"]), D("91000"))
        self.assertEqual(D(pinewell["class_allocations"]["Series A"]), 0)
        self.assertIn("ARR_LARGE_CHANGE", codes(pinewell))
        self.assertIn("-99.896%", next(w["message"] for w in pinewell["warnings"]
                                      if w["code"] == "ARR_LARGE_CHANGE"))
        self.assertIn("PREFERENCE_EFFECT", codes(pinewell))
        orbitra = company(report, "Orbitra")
        self.assertIn("NEW_SHARE_CLASS", codes(orbitra))
        self.assertIn("OWNERSHIP_CHANGE", codes(orbitra))
        self.assertIn("ANALYST_NOTE", codes(orbitra))
        self.assertNotIn("PREFERENCE_EFFECT", codes(orbitra))
        self.assertEqual(D(orbitra["enterprise_value"]), D("246000000"))
        json.dumps(report, allow_nan=False)

    def test_all_eight_class_payouts_against_independent_fraction_fixtures(self) -> None:
        # Fractions make the oracle exact while allowing only the tiny residual
        # introduced by the engine's declared 60-digit Decimal calculation.
        tolerance = F(1, 10**45)
        for quarter, cutoff in ((Q2, date(2026, 8, 14)), (Q3, None)):
            report = build_report(self.dataset, quarter, cutoff)
            for row in report["companies"]:
                expected = CLASS_PAYOUT_FIXTURES[str(quarter), row["company"]]
                with self.subTest(quarter=str(quarter), company=row["company"]):
                    self.assertEqual(set(row["class_allocations"]), set(expected))
                    for name, exact_value in expected.items():
                        self.assertLessEqual(abs(F(D(row["class_allocations"][name])) - exact_value), tolerance)
                    self.assertEqual(sum(expected.values()), F(D(row["enterprise_value"])))
                    exact_fund = sum((expected[s["name"]] * F(s["fund_shares"], s["shares"])
                                      for s in row["cap_table"]["active_series"]), F(0))
                    self.assertLessEqual(abs(F(D(row["fund_position_value"])) - exact_fund), tolerance)

    def test_corrected_arr_growth_and_published_mark_have_different_bases(self) -> None:
        row = company(build_report(self.dataset, Q3), "Halcyon Metrics")
        self.assertEqual(row["selected_inputs"]["prior_arr"]["value"], "18500000")
        self.assertAlmostEqual(float(row["arr_growth_pct"]), 13.5135135135, places=9)
        self.assertEqual(row["prior_quarter_mark"], "23684211")
        with localcontext() as context:
            context.prec = 60
            self.assertEqual(D(row["mark_change_pct"]), (D("22105263") / D("23684211") - 1) * 100)

    def test_q2_restatement_keeps_quarter_end_cap_table(self) -> None:
        report = build_report(self.dataset, Q2)
        self.assertEqual(company(report, "Halcyon Metrics")["rounded_fund_position_value"], "21907895")
        orbitra = company(report, "Orbitra")
        self.assertEqual(orbitra["rounded_fund_position_value"], "28333333")
        self.assertEqual([s["name"] for s in orbitra["cap_table"]["excluded_series"]], ["Series C"])
        self.assertIn("PUBLISHED_MARK_DIFFERENCE", codes(orbitra))

    def test_cutoff_includes_committee_revision_on_its_date(self) -> None:
        before = company(build_report(self.dataset, Q3, date(2026, 10, 19)), "Kestrel Labs")
        after = company(build_report(self.dataset, Q3, date(2026, 10, 20)), "Kestrel Labs")
        self.assertEqual(before["rounded_fund_position_value"], "25000000")
        self.assertEqual(after["rounded_fund_position_value"], "22916667")
        self.assertEqual(before["multiple"], "7.0")
        self.assertEqual(after["multiple"], "5.5")
        self.assertEqual(len(before["input_history"]["multiple"]["excluded_after_cutoff"]), 1)

    def test_arr_correction_is_inclusive_and_does_not_depend_on_file_order(self) -> None:
        before = select_latest(self.dataset.kpis, "Halcyon Metrics", Q2, date(2026, 9, 9), label="ARR")
        after = select_latest(self.dataset.kpis, "Halcyon Metrics", Q2, date(2026, 9, 10), label="ARR")
        self.assertEqual(before.selected.value, D("20000000"))
        self.assertEqual(after.selected.value, D("18500000"))
        rng = random.Random(17)
        kpis, multiples, reported = map(list, (self.dataset.kpis, self.dataset.multiples, self.dataset.reported))
        for records in (kpis, multiples, reported):
            rng.shuffle(records)
        shuffled = replace(self.dataset, kpis=tuple(kpis), multiples=tuple(multiples), reported=tuple(reported))
        self.assertEqual(build_report(shuffled, Q3), build_report(self.dataset, Q3))

    def test_result_does_not_depend_on_ambient_decimal_precision(self) -> None:
        expected = build_report(self.dataset, Q3)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(build_report(self.dataset, Q3), expected)

    def test_historical_publication_mismatch_fails(self) -> None:
        first = self.dataset.reported[0]
        changed = replace(self.dataset, reported=(replace(first, value=first.value + 1), *self.dataset.reported[1:]))
        with self.assertRaisesRegex(MarksError, "historical reproduction failed"):
            build_report(changed, Q2, date(2026, 8, 14))

    def test_missing_current_input_is_visible_but_premature_cutoff_fails(self) -> None:
        with self.assertRaisesRegex(MarksError, "precedes.*quarter-end"):
            build_report(self.dataset, Q3, date(2026, 9, 29))
        report = build_report(self.dataset, Q3, date(2026, 10, 15))
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(any("multiple" in row["missing_inputs"] for row in report["companies"]))
        report = build_report(replace(self.dataset, kpis=()), Q3)
        self.assertEqual(report["marked_company_count"], 0)
        self.assertTrue(all(row["status"] == "no_mark" for row in report["companies"]))

    def test_prior_recalculations_disclose_only_changed_published_marks(self) -> None:
        report = build_report(self.dataset, Q3)
        self.assertFalse(report["hypothetical"])
        self.assertEqual(report["overrides"], [])
        changed = {row["company"]: row["prior_recalculation"] for row in report["companies"]
                   if "PRIOR_MARK_RECALCULATED" in codes(row)}
        self.assertEqual(set(changed), {"Halcyon Metrics", "Orbitra"})
        self.assertEqual(changed["Halcyon Metrics"]["delta"], "-1776316")
        self.assertEqual(changed["Orbitra"]["delta"], "1666666")
        self.assertEqual(changed["Halcyon Metrics"]["calculation"]["rounded_fund_position_value"], "21907895")
        orbitra = changed["Orbitra"]
        self.assertEqual(orbitra["calculation"]["rounded_fund_position_value"], "28333333")
        self.assertEqual([s["name"] for s in orbitra["calculation"]["cap_table"]["excluded_series"]], ["Series C"])
        self.assertEqual(orbitra["selected_inputs"]["multiple"]["value"], "8.5")
        self.assertEqual(orbitra["published_mark"], "26666667")
        for row in report["companies"]:
            self.assertEqual(row["equity_value"], row["enterprise_value"])
            self.assertEqual(row["net_cash"], "0")
            self.assertEqual(row["prior_quarter_mark"], row["prior_recalculation"]["published_mark"])

    def test_prior_multiple_corrections_and_duplicates_retain_all_evidence(self) -> None:
        original = next(row for row in self.dataset.multiples
                        if row.company == "Orbitra" and row.quarter == Q2 and row.value == D("8.5"))
        duplicate = replace(original, source=replace(original.source, row_number=999))
        dataset = replace(self.dataset, multiples=(*self.dataset.multiples, duplicate))
        diagnostic = company(build_report(dataset, Q3), "Orbitra")["prior_recalculation"]
        history = diagnostic["input_history"]["multiple"]
        self.assertEqual([row["value"] for row in history["eligible"]], ["8.0", "8.5", "8.5"])
        self.assertEqual(history["identical_duplicates_ignored"], [duplicate.to_dict()])
        self.assertEqual(diagnostic["selected_inputs"]["multiple"], original.to_dict())

    def test_consecutive_declines_use_the_same_cutoff(self) -> None:
        original = next(row for row in self.dataset.kpis
                        if row.company == "Kestrel Labs" and str(row.quarter) == "2026Q1")
        revised = replace(original, value=D("5000000"), known_on=date(2026, 10, 21),
                          source=replace(original.source, row_number=999))
        dataset = replace(self.dataset, kpis=(*self.dataset.kpis, revised))
        old = company(build_report(dataset, Q3, date(2026, 10, 20)), "Kestrel Labs")
        new = company(build_report(dataset, Q3, date(2026, 10, 21)), "Kestrel Labs")
        self.assertIn("ARR_CONSECUTIVE_DECLINES", codes(old))
        self.assertEqual([row["value"] for row in old["arr_decline_history"]], ["13000000", "16000000"])
        self.assertNotIn("ARR_CONSECUTIVE_DECLINES", codes(new))
        self.assertIn("ARR_DECLINE", codes(new))


class PriorDiagnosticTests(unittest.TestCase):
    def test_optional_missing_prior_inputs_or_cap_do_not_block_current_mark(self) -> None:
        base = simple_dataset()
        prior_multiple = observation("1", Q2, date(2026, 7, 1), filename="valuation_inputs.csv", row=3)
        examples = [base, simple_dataset(prior_arr=None),
                    replace(base, multiples=(*base.multiples, prior_multiple), cap_tables={"Example": CapTable(0, (
                        Series("New", 1, D("1"), date(2026, 7, 1), 1, 1),))})]
        for dataset in examples:
            with self.subTest(dataset=dataset):
                row = build_report(dataset, Q3)["companies"][0]
                self.assertFalse(row["prior_recalculation"]["available"])
                self.assertIsNotNone(row["prior_recalculation"]["reason"])
                self.assertNotIn("PRIOR_MARK_RECALCULATED", codes(row))
                self.assertIsNotNone(row["rounded_fund_position_value"])

    def test_prior_calculation_and_current_streams_have_inclusive_zero_exclusion_diagnostics(self) -> None:
        dataset = simple_dataset()
        prior_multiple = observation("1", Q2, date(2026, 7, 1), filename="valuation_inputs.csv", row=3)
        future = lambda q, filename, row: observation("0", q, date(2026, 10, 2), filename=filename, row=row)
        dataset = replace(dataset,
            kpis=(*dataset.kpis, future(Q3, "kpis.csv", 4), future(Q2, "kpis.csv", 5)),
            multiples=(*dataset.multiples, prior_multiple,
                       future(Q3, "valuation_inputs.csv", 4), future(Q2, "valuation_inputs.csv", 5)),
            reported=(*dataset.reported, future(Q3, "q2_marks.csv", 3), future(Q2, "q2_marks.csv", 4)))
        old = build_report(dataset, Q3, date(2026, 10, 1))["companies"][0]
        excluded = [warning for warning in old["warnings"] if warning["code"] == "INPUT_AFTER_CUTOFF"]
        self.assertEqual(len(excluded), 6)
        self.assertTrue(all("excluded 1 later record(s)" in warning["message"] for warning in excluded))
        self.assertTrue(all("0 recorded 2026-10-02" in warning["message"] for warning in excluded))
        self.assertEqual(old["prior_recalculation"]["calculation"]["rounded_fund_position_value"], "50")
        self.assertEqual(old["prior_recalculation"]["input_history"]["multiple"]["excluded_after_cutoff"][0]["value"], "0")
        new = build_report(dataset, Q3, date(2026, 10, 2))["companies"][0]
        self.assertNotIn("INPUT_AFTER_CUTOFF", codes(new))
        self.assertEqual(new["prior_recalculation"]["calculation"]["rounded_fund_position_value"], "0")
        self.assertEqual(new["rounded_fund_position_value"], "0")

    def test_ambiguous_optional_prior_multiple_remains_an_error(self) -> None:
        dataset = simple_dataset()
        rows = tuple(observation(value, Q2, date(2026, 7, 1), filename="valuation_inputs.csv", row=i)
                     for i, value in enumerate(("1", "2"), 3))
        with self.assertRaisesRegex(MarksError, "prior multiple.*conflicting versions"):
            build_report(replace(dataset, multiples=(*dataset.multiples, *rows)), Q3)


class SelectionConflictTests(unittest.TestCase):
    def test_conflict_at_older_eligible_date_is_not_hidden_by_newer_version(self) -> None:
        rows = [observation("10"), observation("11", row=3),
                observation("12", known=date(2026, 10, 2), row=4)]
        with self.assertRaisesRegex(MarksError, "conflicting versions"):
            select_latest(rows, "Example", Q3, None, label="ARR")

    def test_future_conflict_does_not_break_earlier_cutoff(self) -> None:
        rows = [observation("10"), observation("11", known=date(2026, 10, 2), row=3),
                observation("12", known=date(2026, 10, 2), row=4)]
        selection = select_latest(rows, "Example", Q3, date(2026, 10, 1), label="ARR")
        self.assertEqual(selection.selected.value, D("10"))
        self.assertEqual(len(selection.excluded), 2)

    def test_equal_values_with_different_metadata_are_not_arbitrarily_selected(self) -> None:
        row = observation("10")
        conflict = replace(row, source=SourceRow("kpis.csv", 3, (("value", "10"), ("note", "different"))))
        with self.assertRaisesRegex(MarksError, "source metadata"):
            select_latest((row, conflict), "Example", Q3, None, label="ARR")

    def test_identical_duplicates_are_explicit_and_deterministic(self) -> None:
        dataset = simple_dataset()
        original = dataset.kpis[0]
        duplicate = replace(original, source=replace(original.source, row_number=99))
        report = build_report(replace(dataset, kpis=(duplicate, *dataset.kpis)), Q3)
        row = report["companies"][0]
        self.assertIn("DUPLICATE_SOURCE_ROWS", codes(row))
        self.assertEqual(row["selected_inputs"]["arr"]["source"]["row_number"], 2)
        self.assertEqual(row["input_history"]["arr"]["identical_duplicates_ignored"][0]["source"]["row_number"], 99)


class FinancialBoundaryTests(unittest.TestCase):
    def test_two_for_one_split_preserves_supplied_company_economics(self) -> None:
        original = load_dataset(DATA)
        sources = dict(original.sources)
        caps = json.loads(sources["cap_tables.json"])
        # A consistently adjusted split changes share units, not economics.
        # Parse the changed source so this also exercises the input boundary.
        for cap in caps.values():
            cap["common_shares"] *= 2
            for series in cap["series"]:
                series["shares"] *= 2
                series["fund_shares"] *= 2
                series["issue_price"] = str(D(series["issue_price"]) / 2)
        sources["cap_tables.json"] = json.dumps(caps)
        split = parse_sources(sources)
        for quarter, cutoff in ((Q2, date(2026, 8, 14)), (Q3, None)):
            before = build_report(original, quarter, cutoff)
            after = build_report(split, quarter, cutoff)
            for old, new in zip(before["companies"], after["companies"]):
                with self.subTest(quarter=str(quarter), company=old["company"]):
                    self.assertEqual(old["company"], new["company"])
                    for field in ("enterprise_value", "equity_value", "ownership_pct",
                                  "rounded_fund_position_value", "mark_change_pct"):
                        self.assertEqual(old[field], new[field])
                    for field in ("class_allocations", "fund_contributions"):
                        self.assertEqual(set(old[field]), set(new[field]))
                        for name in old[field]:
                            self.assertLessEqual(abs(F(D(old[field][name])) - F(D(new[field][name]))),
                                                 F(1, 10**45))

    def test_reducing_holdings_changes_fund_value_not_class_allocations(self) -> None:
        original = load_dataset(DATA)
        before = build_report(original, Q3)
        for numerator in (0, 1):
            sources = dict(original.sources)
            caps = json.loads(sources["cap_tables.json"])
            for cap in caps.values():
                for series in cap["series"]:
                    self.assertEqual(series["fund_shares"] % 2, 0)
                    series["fund_shares"] = series["fund_shares"] // 2 * numerator
            sources["cap_tables.json"] = json.dumps(caps)
            after = build_report(parse_sources(sources), Q3)
            for old, new in zip(before["companies"], after["companies"]):
                with self.subTest(company=old["company"], holding_fraction=F(numerator, 2)):
                    for field in ("company", "enterprise_value", "equity_value", "class_allocations"):
                        self.assertEqual(old[field], new[field])
                    for field in ("fund_position_value", "ownership_pct"):
                        # Scale unrounded values: separately rounded marks need not scale exactly.
                        expected = F(D(old[field])) * F(numerator, 2)
                        self.assertLessEqual(abs(F(D(new[field])) - expected), F(1, 10**45))

    def test_actual_preference_and_conversion_boundaries_match_exact_finance(self) -> None:
        dataset = load_dataset(DATA)
        # Independent piecewise formulas from the supplied shares/rights.
        # Kestrel: equal-priority 60m claims; fund owns half of B's 50m claim.
        # After full preference coverage, B keeps 50m until conversion at 200m.
        def kestrel(equity: int) -> Fraction:
            if equity <= 60_000_000:
                return F(equity * 5, 12)
            return F(25_000_000) if equity <= 200_000_000 else F(equity, 8)

        # Pinewell: senior B gets 36m first; A has a 12m preference, half held
        # by the fund. A converts at 72m; B converts at 144m.
        def pinewell(equity: int) -> Fraction:
            if equity <= 36_000_000:
                return F(0)
            if equity <= 48_000_000:
                return F(equity - 36_000_000, 2)
            if equity <= 72_000_000:
                return F(6_000_000)
            if equity <= 144_000_000:
                return F(equity - 36_000_000, 6)
            return F(equity, 8)

        cases = (("Kestrel Labs", kestrel, (60_000_000, 80_000_000, 200_000_000)),
                 ("Pinewell", pinewell, (36_000_000, 48_000_000, 72_000_000, 144_000_000)))
        for name, expected_value, thresholds in cases:
            values = sorted({0, 91_000, 55_000_000, 91_000_000, 300_000_000,
                             *(point + offset for point in thresholds for offset in (-1, 0, 1))})
            previous = F(0)
            for equity in values:
                overrides = [{"company": name, "field": "arr", "value": str(equity)},
                             {"company": name, "field": "multiple", "value": "1"}]
                row = company(build_report(dataset, Q3, overrides=overrides), name)
                expected = expected_value(equity)
                actual = F(D(row["fund_position_value"]))
                with self.subTest(company=name, equity=equity):
                    self.assertEqual(D(row["equity_value"]), equity)
                    self.assertLessEqual(abs(actual - expected), F(1, 10**45))
                    # Exact positive half-up rounding, independent of Decimal context.
                    rounded = (2 * expected.numerator + expected.denominator) // (2 * expected.denominator)
                    self.assertEqual(row["rounded_fund_position_value"], str(rounded))
                    self.assertGreaterEqual(actual, previous)
                previous = actual

    def test_seeded_valid_caps_conserve_value_and_fund_bounds_under_permutations(self) -> None:
        rng = random.Random(84)
        tolerance = F(1, 10**45)
        # Small bounded coverage across ties, seniority tiers, zero equity,
        # preference shortfalls and conversion. No replacement waterfall oracle.
        for case in range(24):
            series = []
            for index in range(1 + case % 4):
                shares = rng.randint(1, 50)
                series.append(Series(chr(65 + index), shares, D(rng.randint(1, 30)),
                                     date(2025, 1, 1), rng.randint(1, 3), rng.randint(0, shares)))
            cap = CapTable(rng.randint(0, 50), tuple(series))
            equity = 0 if case == 0 else rng.randint(1, 6000)
            dataset = simple_dataset(cap=cap, arr=str(equity))
            reference = build_report(dataset, Q3)
            row = reference["companies"][0]
            allocations = {name: F(D(value)) for name, value in row["class_allocations"].items()}
            contributions = {name: F(D(value)) for name, value in row["fund_contributions"].items()}
            with self.subTest(case=case, equity=equity):
                self.assertLessEqual(abs(sum(allocations.values()) - equity), tolerance)
                self.assertTrue(all(value >= 0 for value in allocations.values()))
                for name, value in contributions.items():
                    self.assertGreaterEqual(value, 0)
                    self.assertLessEqual(value - allocations[name], tolerance)
                fund = F(D(row["fund_position_value"]))
                self.assertGreaterEqual(fund, 0)
                self.assertLessEqual(fund - equity, tolerance)
                self.assertLessEqual(abs(sum(contributions.values()) - fund), tolerance)
                for ordered in itertools.permutations(series):
                    permuted = replace(dataset, cap_tables={"Example": CapTable(cap.common_shares, ordered)})
                    self.assertEqual(build_report(permuted, Q3), reference)

    def test_missing_and_zero_prior_baselines_are_distinct(self) -> None:
        missing = build_report(simple_dataset(prior_arr=None, prior_mark=None), Q3)["companies"][0]
        zero = build_report(simple_dataset(prior_arr="0", prior_mark="0"), Q3)["companies"][0]
        for row in (missing, zero):
            self.assertIsNone(row["arr_growth_pct"])
            self.assertIsNone(row["mark_change_pct"])
        self.assertIn("MISSING_PRIOR_ARR", codes(missing))
        self.assertIn("ZERO_PRIOR_ARR", codes(zero))
        self.assertIsNone(missing["prior_quarter_mark"])
        self.assertEqual(zero["prior_quarter_mark"], "0")

    def test_common_only_cap_has_zero_fund_value_and_conserves_equity(self) -> None:
        row = build_report(simple_dataset(cap=CapTable(10, ())), Q3)["companies"][0]
        self.assertEqual(D(row["class_allocations"]["Common"]), D("100"))
        self.assertEqual(row["rounded_fund_position_value"], "0")
        self.assertEqual(D(row["ownership_pct"]), 0)

    def test_cap_with_only_future_shares_is_rejected_after_filtering(self) -> None:
        cap = CapTable(0, (Series("Future", 10, D("1"), date(2026, 10, 1), 1, 5),))
        with self.assertRaisesRegex(MarksError, "no outstanding shares at quarter-end"):
            build_report(simple_dataset(cap=cap), Q3)

    def test_issue_on_quarter_end_is_included(self) -> None:
        cap = CapTable(0, (Series("New", 10, D("1"), date(2026, 9, 30), 1, 5),))
        row = build_report(simple_dataset(cap=cap), Q3)["companies"][0]
        self.assertEqual(row["rounded_fund_position_value"], "50")
        self.assertEqual(row["cap_table"]["excluded_series"], [])

    def test_zero_equity_is_real_zero_not_missing(self) -> None:
        row = build_report(simple_dataset(arr="0"), Q3)["companies"][0]
        self.assertEqual(row["rounded_fund_position_value"], "0")
        self.assertEqual(D(row["mark_change_pct"]), -100)
        self.assertTrue(all(D(value) == 0 for value in row["class_allocations"].values()))

    def test_half_dollar_final_value_rounds_up(self) -> None:
        row = build_report(simple_dataset(arr="5"), Q3)["companies"][0]
        self.assertEqual(D(row["fund_position_value"]), D("2.5"))
        self.assertEqual(row["rounded_fund_position_value"], "3")

    def test_equal_seniority_underfunding_pays_claims_proportionally(self) -> None:
        cap = CapTable(10, (Series("A", 5, D("2"), date(2025, 1, 1), 1, 0),
                            Series("B", 5, D("10"), date(2025, 1, 1), 1, 5)))
        row = build_report(simple_dataset(cap=cap, arr="30"), Q3)["companies"][0]
        self.assertEqual(D(row["class_allocations"]["A"]), 5)
        self.assertEqual(D(row["class_allocations"]["B"]), 25)
        self.assertEqual(row["rounded_fund_position_value"], "25")

    def test_invalid_allocator_results_fail_before_report(self) -> None:
        bad_results = [
            {"Common": D("50")},
            {"Common": D("60"), "A": D("50")},
            {"Common": D("110"), "A": D("-10")},
            {"Common": D("NaN"), "A": D("50")},
            {"Common": D("Infinity"), "A": D("50")},
        ]
        for allocations in bad_results:
            with self.subTest(allocations=allocations), patch(
                "quarterly_marks.valuation.waterfall.allocate", return_value=allocations
            ), self.assertRaises(MarksError):
                build_report(simple_dataset(), Q3)

    def test_year_one_has_no_previous_quarter(self) -> None:
        quarter = Quarter.parse("0001Q1")
        dataset = simple_dataset(prior_arr=None, prior_mark=None)
        dataset = replace(dataset,
                          kpis=(replace(dataset.kpis[0], quarter=quarter, known_on=date(1, 4, 1)),),
                          multiples=(replace(dataset.multiples[0], quarter=quarter, known_on=date(1, 4, 1)),),
                          cap_tables={"Example": CapTable(1, ())})
        row = build_report(dataset, quarter)["companies"][0]
        self.assertEqual(row["arr_growth_reason"], "No earlier calendar quarter")
        self.assertIsNone(row["prior_quarter_mark"])


if __name__ == "__main__":
    unittest.main()
