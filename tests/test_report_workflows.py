"""Ordered attribution and incomplete reports must stay honest through replay."""

import copy
import json
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path
import shutil
import tempfile
import unittest

from quarterly_marks import audit
from quarterly_marks.domain import AuditError, Quarter
from quarterly_marks.inputs import load_dataset, parse_sources
from quarterly_marks.scenarios import parse_overrides
from quarterly_marks.valuation import build_report
from test_audit import reseal
import test_cli as cli
from test_cli import company


ROOT = Path(__file__).resolve().parents[1]
Q2, Q3 = Quarter.parse("2026Q2"), Quarter.parse("2026Q3")
D = Decimal


class ReportWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(ROOT / "data")

    def missing(self, fields=("arr",), company_name="Halcyon Metrics"):
        sources = dict(self.dataset.sources)
        for field in fields:
            name = "kpis.csv" if field == "arr" else "valuation_inputs.csv"
            sources[name] = "\n".join(line for line in sources[name].splitlines()
                                        if not line.startswith(company_name + ",2026Q3,")) + "\n"
        return parse_sources(sources)

    def test_bridge_reconciles_all_companies_to_displayed_change(self):
        report = build_report(self.dataset, Q3, include_attribution=True)
        for row in report["companies"]:
            bridge = row["attribution"]
            with self.subTest(company=row["company"]):
                self.assertTrue(bridge["available"])
                self.assertEqual([step["step"] for step in bridge["steps"]], bridge["order"])
                effects = sum(D(step["effect"]) for step in bridge["steps"][1:])
                self.assertEqual(effects, D(row["rounded_fund_position_value"]) - D(row["prior_quarter_mark"]))
                self.assertEqual(str(effects), bridge["total_change"])
                self.assertIn("Order-dependent", bridge["caveat"])
        orbitra = company(report, "Orbitra")["attribution"]
        self.assertEqual([step["fund_value"] for step in orbitra["steps"]],
                         ["26666667", "28333333", "34000000", "41000000", "36000000", "36000000"])
        self.assertEqual(orbitra["steps"][4]["effect"], "-5000000")

    def test_kestrel_is_an_interaction_not_a_unique_multiple_effect(self):
        bridge = company(build_report(self.dataset, Q3, include_attribution=True), "Kestrel Labs")["attribution"]
        interaction = bridge["interaction"]
        self.assertEqual(interaction["arr_alone_effect"], "0")
        self.assertEqual(interaction["multiple_alone_effect"], "0")
        self.assertEqual(interaction["joint_effect"], "-2083333")
        self.assertEqual(interaction["interaction_effect"], "-2083333")
        self.assertEqual(interaction["multiple_only_calculation"]["equity_value"], "71500000.0")
        # Reversing the two steps would assign the entire decline to ARR.
        self.assertEqual(D(bridge["steps"][3]["fund_value"]) -
                         D(interaction["multiple_only_calculation"]["rounded_fund_position_value"]), D(-2083333))

    def test_attribution_uses_cutoff_and_prior_quarter_capitalization(self):
        report = build_report(self.dataset, Q3, date(2026, 10, 16), include_attribution=True)
        self.assertEqual(company(report, "Kestrel Labs")["attribution"]["total_change"], "0")
        bridge = company(report, "Orbitra")["attribution"]
        self.assertEqual(len(bridge["steps"][3]["calculation"]["cap_table"]["active_series"]), 2)
        self.assertEqual(len(bridge["steps"][4]["calculation"]["cap_table"]["active_series"]), 3)

    def test_net_cash_has_its_own_step_and_scenarios_preserve_prior(self):
        report = build_report(self.dataset, Q3, overrides=parse_overrides([
            "Kestrel Labs.net_cash=-5000000", "Pinewell.arr=13000000"]), include_attribution=True)
        bridge = company(report, "Kestrel Labs")["attribution"]
        self.assertEqual(bridge["steps"][4]["effect"], "0")
        self.assertEqual(bridge["steps"][5]["effect"], "-2083334")
        self.assertEqual(bridge["steps"][-1]["fund_value"], "20833333")
        pinewell = company(report, "Pinewell")
        self.assertEqual(pinewell["attribution"]["steps"][1]["calculation"]["arr"], "12500000")
        self.assertEqual(pinewell["attribution"]["steps"][-1]["fund_value"], "9166667")

    def test_unavailable_prior_does_not_fabricate_zero_attribution(self):
        for data, quarter in ((self.dataset, Q2), (self.missing(), Q3)):
            report = build_report(data, quarter, include_attribution=True)
            bridge = company(report, "Halcyon Metrics")["attribution"]
            self.assertFalse(bridge["available"])
            self.assertTrue(bridge["reason"])
            self.assertEqual(bridge["steps"], [])
            self.assertIsNone(bridge["total_change"])
        sources = dict(self.dataset.sources)
        sources["q2_marks.csv"] = sources["q2_marks.csv"].splitlines()[0] + "\n"
        row = company(build_report(parse_sources(sources), Q3, include_attribution=True), "Orbitra")
        self.assertTrue(row["prior_recalculation"]["available"])
        self.assertFalse(row["attribution"]["available"])
        sources = dict(self.dataset.sources)
        sources["q2_marks.csv"] = sources["q2_marks.csv"].replace(
            "Halcyon Metrics,2026Q2,23684211", "Halcyon Metrics,2026Q2,0")
        zero = company(build_report(parse_sources(sources), Q3, include_attribution=True), "Halcyon Metrics")
        self.assertTrue(zero["attribution"]["available"])
        self.assertEqual(zero["attribution"]["total_change"], "22105263")

    def test_attribution_ignores_ambient_decimal_context(self):
        expected = build_report(self.dataset, Q3, include_attribution=True)
        with localcontext() as context:
            context.prec = 3
            context.rounding = "ROUND_DOWN"
            actual = build_report(self.dataset, Q3, include_attribution=True)
            audit.create_receipt(self.dataset, actual)
        self.assertEqual(actual, expected)

    def test_missing_fields_preserve_other_companies_and_known_evidence(self):
        baseline = build_report(self.dataset, Q3)
        for fields in (("arr",), ("multiple",), ("arr", "multiple")):
            report = build_report(self.missing(fields), Q3)
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["marked_company_count"], 3)
            row = company(report, "Halcyon Metrics")
            self.assertEqual(row["missing_inputs"], list(fields))
            self.assertEqual(row["status"], "no_mark")
            self.assertTrue(row["errors"])
            self.assertIsNone(row["rounded_fund_position_value"])
            self.assertIsNone(row["mark_change_pct"])
            self.assertEqual(row["prior_quarter_mark"], "23684211")
            for name in ("Orbitra", "Kestrel Labs", "Pinewell"):
                # Removing a CSV row shifts evidence line numbers, not the marks.
                for field in ("arr", "multiple", "fund_position_value", "rounded_fund_position_value",
                              "prior_quarter_mark", "arr_growth_pct", "mark_change_pct"):
                    self.assertEqual(company(report, name)[field], company(baseline, name)[field])

    def test_incomplete_and_attributed_receipts_replay_without_sources(self):
        for dataset in (self.dataset, self.missing()):
            report = build_report(dataset, Q3, include_attribution=True)
            with tempfile.TemporaryDirectory() as directory:
                path = audit.write_receipt(audit.create_receipt(dataset, report), Path(directory))
                self.assertEqual(audit.replay_receipt(path)[1], report)
                self.assertTrue(audit.load_receipt(path)["request"]["include_attribution"])

    def test_status_and_bridge_corruption_is_rejected_even_when_resealed(self):
        complete = audit.create_receipt(self.dataset, build_report(self.dataset, Q3, include_attribution=True))
        incomplete = audit.create_receipt(self.missing(), build_report(self.missing(), Q3, include_attribution=True))
        mutations = [
            (complete, lambda r: r["report"].update(status="incomplete")),
            (complete, lambda r: r["report"].update(marked_company_count=True)),
            (complete, lambda r: r["request"].update(include_attribution=False)),
            (complete, lambda r: r["report"]["companies"][0]["attribution"]["steps"][2].update(effect="0")),
            (complete, lambda r: r["report"]["companies"][0]["attribution"]["interaction"].update(joint_effect="0")),
            (incomplete, lambda r: r["report"]["companies"][0].update(rounded_fund_position_value="0")),
            (incomplete, lambda r: r["report"]["companies"][0].update(errors=[])),
            (incomplete, lambda r: r["report"]["companies"][0].update(status=[])),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "changed.json"
            for original, mutate in mutations:
                changed = copy.deepcopy(original)
                mutate(changed)
                path.write_text(json.dumps(reseal(changed)))
                with self.assertRaises(AuditError):
                    audit.load_receipt(path)


class WorkflowCliTests(unittest.TestCase):
    # Reuse only subprocess setup/helpers, not the parent's test methods.
    setUp = cli.CliTests.setUp
    run_cli = cli.CliTests.run_cli
    success_json = cli.CliTests.success_json
    copy_inputs = cli.CliTests.copy_inputs

    def test_missing_row_prints_and_strict_saves_same_incomplete_report(self):
        inputs = self.copy_inputs()
        path = inputs / "kpis.csv"
        path.write_text("\n".join(line for line in path.read_text().splitlines()
                                  if not line.startswith("Halcyon Metrics,2026Q3,")) + "\n")
        display = self.run_cli("--quarter", "2026Q3", "--data-dir", inputs, "--explain", "Halcyon Metrics")
        self.assertEqual(display.returncode, 0, display.stderr)
        self.assertIn("NO MARK", display.stdout)
        self.assertIn("3 of 4", display.stdout)
        self.assertIn("ERROR", display.stdout)
        ordinary = self.success_json("--quarter", "2026Q3", "--data-dir", inputs, "--attribution")
        strict = self.run_cli("--quarter", "2026Q3", "--data-dir", inputs, "--attribution", "--strict", "--format", "json")
        self.assertEqual(strict.returncode, 2, strict.stderr)
        saved = json.loads(strict.stdout)
        self.assertEqual(saved["report"], ordinary["report"])
        self.assertTrue(Path(saved["audit_file"]).is_file())
        shutil.rmtree(inputs)
        replay = self.success_json("--replay", saved["audit_file"])
        self.assertEqual(replay["report"], saved["report"])
        strict_replay = self.run_cli("--replay", saved["audit_file"], "--strict")
        self.assertEqual(strict_replay.returncode, 2)
        self.assertIn("NO MARK", strict_replay.stdout)

    def test_all_missing_does_not_become_zero_or_a_total_loss(self):
        result = self.run_cli("--quarter", "2026Q3", "--as-of", "2026-10-01", "--strict")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("0 of 4", result.stdout)
        self.assertEqual(result.stdout.count("NO MARK"), 4)
        self.assertNotIn("-100.00%", result.stdout)

    def test_attribution_output_replay_and_comparison(self):
        ordinary = self.success_json("--quarter", "2026Q3")
        attributed = self.success_json("--quarter", "2026Q3", "--attribution")
        text = self.run_cli("--replay", attributed["audit_file"])
        for expected in ("Order:", "Order-dependent", "interaction", "5,000,000", "2,083,333"):
            self.assertIn(expected, text.stdout)
        self.assertEqual(self.success_json("--replay", attributed["audit_file"])["report"], attributed["report"])
        compared = self.success_json("--compare", ordinary["audit_file"], attributed["audit_file"])
        self.assertTrue(compared["comparison"]["has_changes"])
        self.assertEqual(self.success_json("--replay", compared["audit_file"])["comparison"], compared["comparison"])
        for flags in (("--replay", attributed["audit_file"], "--attribution"),
                      ("--compare", ordinary["audit_file"], attributed["audit_file"], "--strict")):
            rejected = self.run_cli(*flags)
            self.assertEqual(rejected.returncode, 2)
            self.assertEqual(rejected.stdout, "")


if __name__ == "__main__":
    unittest.main()
