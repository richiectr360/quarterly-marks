"""Subprocess acceptance tests: the actual commands, artifacts, and failure modes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "marks.py"
EXPECTED_Q2 = {"Halcyon Metrics": "23684211", "Orbitra": "26666667",
               "Pinewell": "14833333", "Kestrel Labs": "25000000"}
EXPECTED_Q3 = {"Halcyon Metrics": "22105263", "Orbitra": "36000000",
               "Pinewell": "0", "Kestrel Labs": "22916667"}
SOURCE_FILES = ("kpis.csv", "valuation_inputs.csv", "cap_tables.json", "q2_marks.csv")


def company(report, name):
    return next(row for row in report["companies"] if row["company"] == name)


def save_resealed(path, receipt):
    unsigned = {key: value for key, value in receipt.items() if key != "integrity_sha256"}
    receipt["integrity_sha256"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")).hexdigest()
    path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.audits = self.work / "audits"

    def run_cli(self, *arguments, audit_dir=None):
        command = [sys.executable, "-B", str(SCRIPT), *map(str, arguments),
                   "--audit-dir", str(audit_dir or self.audits)]
        return subprocess.run(command, cwd=self.work, capture_output=True, text=True,
                              timeout=20, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def success_json(self, *arguments):
        completed = self.run_cli(*arguments, "--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(Path(result["audit_file"]).is_file())
        self.assertIn("Audit saved:", completed.stderr)
        return result

    def copy_inputs(self):
        data = self.work / "editable-data"
        data.mkdir()
        for filename in SOURCE_FILES:
            shutil.copyfile(ROOT / "data" / filename, data / filename)
        return data

    def test_both_required_commands_and_exact_marks(self):
        for args, expected in [(("--quarter", "2026Q3"), EXPECTED_Q3),
                               (("--quarter", "2026Q2", "--as-of", "2026-08-14"), EXPECTED_Q2)]:
            with self.subTest(args=args):
                output = self.success_json(*args)
                report = output["report"]
                actual = {row["company"]: row["rounded_fund_position_value"] for row in report["companies"]}
                self.assertEqual(actual, expected)
                if report["quarter"] == "2026Q2":
                    self.assertTrue(all(row["prior_quarter_mark"] is None for row in report["companies"]))
        self.assertEqual(len(list(self.audits.glob("*.json"))), 2)

    def test_text_report_has_required_columns_and_explanation(self):
        completed = self.run_cli("--quarter", "2026Q3", "--explain", "Pinewell")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for text in ("ARR QoQ", "Multiple", "Enterprise value", "Ownership", "Fund value",
                     "Prior published", "Mark change", "EXPLANATION | Pinewell", "13,000",
                     "Series B", "Series A", "kpis.csv:11", "REVIEW", "-99.896"):
            self.assertIn(text, completed.stdout)

    def test_current_report_discloses_both_prior_quarter_corrections(self):
        report = self.success_json("--quarter", "2026Q3")["report"]
        for name, published, recalculated, field, date in (
                ("Halcyon Metrics", "23,684,211", "21,907,895", "arr", "2026-09-10"),
                ("Orbitra", "26,666,667", "28,333,333", "multiple", "2026-09-05")):
            with self.subTest(company=name):
                row = company(report, name)
                messages = "\n".join(warning["message"] for warning in row["warnings"])
                self.assertIn(published, messages)
                self.assertIn(recalculated, messages)
                self.assertEqual(row["prior_recalculation"]["selected_inputs"][field]["known_on"], date)

    def test_future_zero_arr_is_excluded_without_crashing_or_changing_the_mark(self):
        data = self.copy_inputs()
        path = data / "kpis.csv"
        path.write_text(path.read_text() + "Pinewell,2026Q3,0,2026-10-30\n", encoding="utf-8")
        report = self.success_json("--quarter", "2026Q3", "--as-of", "2026-10-20", "--data-dir", data)["report"]
        row = company(report, "Pinewell")
        self.assertEqual(row["arr"], "13000")
        self.assertEqual(row["rounded_fund_position_value"], "0")
        excluded = row["input_history"]["arr"]["excluded_after_cutoff"]
        self.assertEqual([(item["value"], item["known_on"]) for item in excluded], [("0", "2026-10-30")])

    def test_temporary_csvs_are_editable_without_a_manifest(self):
        data = self.copy_inputs()
        self.assertFalse((data / "batch-manifest.json").exists())
        original = self.success_json("--quarter", "2026Q3", "--data-dir", data)
        path = data / "kpis.csv"
        path.write_text(path.read_text().replace("Pinewell,2026Q3,13000,", "Pinewell,2026Q3,13000000,"),
                        encoding="utf-8")
        changed = self.success_json("--quarter", "2026Q3", "--data-dir", data)
        self.assertEqual(company(original["report"], "Pinewell")["rounded_fund_position_value"], "0")
        self.assertEqual(company(changed["report"], "Pinewell")["rounded_fund_position_value"], "9166667")
        self.assertFalse(changed["report"]["hypothetical"])
        self.assertEqual(changed["report"]["overrides"], [])

    def test_default_sources_resolve_from_script_not_working_directory(self):
        self.assertFalse((self.work / "data").exists())
        output = self.success_json("--quarter", "2026Q3")
        self.assertEqual(len(output["report"]["companies"]), 4)

    def test_replay_uses_captured_sources_after_directory_removed(self):
        data = self.work / "copied-data"
        shutil.copytree(ROOT / "data", data)
        original = self.success_json("--quarter", "2026Q3", "--data-dir", data)
        shutil.rmtree(data)
        replayed = self.success_json("--replay", original["audit_file"])
        self.assertEqual(replayed["report"], original["report"])
        self.assertNotEqual(replayed["run_id"], original["run_id"])
        receipt = json.loads(Path(replayed["audit_file"]).read_text())
        self.assertEqual(receipt["operation"], "replay")
        self.assertEqual(receipt["parent_run_ids"], [original["run_id"]])

    def test_cross_runtime_replay_warns_on_stderr_and_preserves_lineage(self):
        original = self.success_json("--quarter", "2026Q3")
        path = Path(original["audit_file"])
        receipt = json.loads(path.read_text())
        current_runtime = dict(receipt["engine"]["runtime"])
        major, minor, patch = current_runtime["python"].split(".")
        receipt["engine"]["runtime"]["python"] = f"{major}.{int(minor) + 1}.{patch}"
        save_resealed(path, receipt)
        original_bytes = path.read_bytes()
        completed = self.run_cli("--replay", path, "--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replayed = json.loads(completed.stdout)
        self.assertEqual(replayed["report"], original["report"])
        self.assertIn("RUNTIME_DIFFERENCE", completed.stderr)
        self.assertNotIn("RUNTIME_DIFFERENCE", completed.stdout)
        saved = json.loads(Path(replayed["audit_file"]).read_text())
        self.assertEqual(saved["engine"]["runtime"], current_runtime)
        self.assertEqual(saved["parent_run_ids"], [original["run_id"]])
        self.assertNotEqual(saved["run_id"], original["run_id"])
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_incompatible_runtime_replay_fails_cleanly_without_receipt(self):
        original = self.success_json("--quarter", "2026Q3")
        original_text = Path(original["audit_file"]).read_text()
        for field in ("implementation", "python"):
            with self.subTest(field=field):
                receipt = json.loads(original_text)
                runtime = receipt["engine"]["runtime"]
                runtime[field] = ("OtherPython" if field == "implementation" else
                                  str(int(runtime["python"].split(".")[0]) + 1) + ".0.0")
                path = self.work / "incompatible.json"
                save_resealed(path, receipt)
                completed = self.run_cli("--replay", path, "--format", "json")
                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertIn("AUDIT_ERROR", completed.stderr)
                self.assertIn("incompatible", completed.stderr)
                self.assertNotIn("RUNTIME_DIFFERENCE", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(len(list(self.audits.glob("*.json"))), 1)

    def test_hypothetical_override_is_labelled_and_explains_original_input(self):
        completed = self.run_cli("--quarter", "2026Q3", "--override", "Pinewell.arr=13000000",
                                 "--explain", "Pinewell")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        for expected in ("HYPOTHETICAL", "9,166,667", "13,000", "13,000,000", "kpis.csv:11"):
            self.assertIn(expected, completed.stdout)

    def test_hypothetical_snapshot_replay_survives_removed_input_directory(self):
        data = self.copy_inputs()
        original_bytes = {name: (data / name).read_bytes() for name in SOURCE_FILES}
        original = self.success_json("--quarter", "2026Q3", "--data-dir", data,
                                     "--override", "Pinewell.arr=13000000.00")
        expected = [{"company": "Pinewell", "field": "arr", "value": "13000000"}]
        self.assertTrue(original["report"]["hypothetical"])
        self.assertEqual(original["report"]["overrides"], expected)
        pinewell = company(original["report"], "Pinewell")
        self.assertEqual(pinewell["rounded_fund_position_value"], "9166667")
        self.assertEqual(pinewell["selected_inputs"]["arr"]["value"], "13000")
        self.assertEqual({name: (data / name).read_bytes() for name in SOURCE_FILES}, original_bytes)
        shutil.rmtree(data)
        replayed = self.success_json("--replay", original["audit_file"])
        self.assertEqual(replayed["report"], original["report"])
        receipt = json.loads(Path(replayed["audit_file"]).read_text())
        self.assertEqual(receipt["request"]["overrides"], expected)
        self.assertTrue(receipt["request"]["hypothetical"])
        self.assertEqual(receipt["parent_run_ids"], [original["run_id"]])

    def test_multiple_and_signed_net_cash_overrides(self):
        for expression, expected in (("Kestrel Labs.multiple=7", "25000000"),
                                     ("Kestrel Labs.net_cash=-5000000", "20833333")):
            with self.subTest(expression=expression):
                result = self.success_json("--quarter", "2026Q3", "--override", expression)
                row = company(result["report"], "Kestrel Labs")
                self.assertTrue(result["report"]["hypothetical"])
                self.assertEqual(row["rounded_fund_position_value"], expected)
                self.assertEqual(row["selected_inputs"]["multiple"]["value"], "5.5")

    def test_override_can_supply_an_absent_original_arr_or_multiple(self):
        for field, filename, value, expected in (("arr", "kpis.csv", "13000000", "9166667"),
                                                ("multiple", "valuation_inputs.csv", "7", "0")):
            with self.subTest(field=field):
                data = self.work / ("missing-" + field)
                data.mkdir()
                for name in SOURCE_FILES:
                    text = (ROOT / "data" / name).read_text()
                    if name == filename:
                        text = "\n".join(line for line in text.splitlines()
                                         if not line.startswith("Pinewell,2026Q3,")) + "\n"
                    (data / name).write_text(text, encoding="utf-8")
                result = self.success_json("--quarter", "2026Q3", "--data-dir", data,
                                           "--override", f"Pinewell.{field}={value}")
                row = company(result["report"], "Pinewell")
                self.assertIsNone(row["selected_inputs"][field])
                self.assertEqual(row["rounded_fund_position_value"], expected)
                replayed = self.success_json("--replay", result["audit_file"])
                self.assertEqual(replayed["report"], result["report"])

    def test_compare_dates_and_replay_comparison(self):
        earlier = self.success_json("--quarter", "2026Q3", "--as-of", "2026-10-16")
        later = self.success_json("--quarter", "2026Q3", "--as-of", "2026-10-20")
        # Comparisons describe archived parents, even when their runtimes cannot revalue here.
        earlier_path = Path(earlier["audit_file"])
        archived = json.loads(earlier_path.read_text())
        archived["engine"]["runtime"] = {"implementation": "OtherPython", "python": "99.0.0"}
        save_resealed(earlier_path, archived)
        compared = self.success_json("--compare", earlier["audit_file"], later["audit_file"])
        serialized = json.dumps(compared["comparison"])
        self.assertIn("Kestrel Labs", serialized)
        self.assertIn("25000000", serialized)
        self.assertIn("22916667", serialized)
        # A comparison receipt is self-contained, including its two parents.
        Path(earlier["audit_file"]).unlink()
        Path(later["audit_file"]).unlink()
        completed = self.run_cli("--replay", compared["audit_file"], "--format", "json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("RUNTIME_DIFFERENCE", completed.stderr)
        reproduced = json.loads(completed.stdout)
        self.assertEqual(reproduced["comparison"], compared["comparison"])

    def test_text_comparison_shows_financial_effect(self):
        earlier = self.success_json("--quarter", "2026Q3", "--as-of", "2026-10-16")
        later = self.success_json("--quarter", "2026Q3", "--as-of", "2026-10-20")
        compared = self.run_cli("--compare", earlier["audit_file"], later["audit_file"])
        self.assertEqual(compared.returncode, 0, compared.stderr)
        for expected in ("Kestrel Labs", "$25,000,000", "$22,916,667", "7.0", "5.5", "Input evidence"):
            self.assertIn(expected, compared.stdout)

    def test_no_op_override_is_distinguished_from_an_actual_run(self):
        actual = self.success_json("--quarter", "2026Q3")
        hypothetical = self.success_json("--quarter", "2026Q3", "--override", "Pinewell.net_cash=0")
        actual_marks = {row["company"]: row["rounded_fund_position_value"] for row in actual["report"]["companies"]}
        hypothetical_marks = {row["company"]: row["rounded_fund_position_value"] for row in hypothetical["report"]["companies"]}
        self.assertEqual(actual_marks, hypothetical_marks)
        compared = self.success_json("--compare", actual["audit_file"], hypothetical["audit_file"])
        comparison = compared["comparison"]
        self.assertTrue(comparison["has_changes"])
        self.assertFalse(comparison["first_request"]["hypothetical"])
        self.assertTrue(comparison["second_request"]["hypothetical"])
        replayed = self.success_json("--replay", compared["audit_file"])
        self.assertEqual(replayed["comparison"], comparison)

    def test_fresh_overrides_are_rejected_on_replay_and_compare(self):
        original = self.success_json("--quarter", "2026Q3")
        path = original["audit_file"]
        for action in (("--replay", path), ("--compare", path, path)):
            with self.subTest(action=action):
                completed = self.run_cli(*action, "--override", "Pinewell.arr=13000000")
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(len(list(self.audits.glob("*.json"))), 1)

    def test_invalid_overrides_do_not_print_marks_or_write_receipts(self):
        invalid = [
            ("Pinewell.arr=13000000", "Pinewell.arr=14000000"),
            ("Unknown Company.arr=13000000",),
            ("Pinewell.revenue=13000000",),
            ("Pinewell.arr=NaN",),
            ("Pinewell.multiple=Infinity",),
            ("Pinewell.net_cash=-Infinity",),
            ("Pinewell.arr=-1",),
            ("Pinewell.multiple=-1",),
            ("Pinewell.net_cash=-1000000",),
            ("Pinewell.arr=",),
            ("Pinewell.arr=not-a-number",),
        ]
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                arguments = ["--quarter", "2026Q3"]
                for override in overrides:
                    arguments.extend(("--override", override))
                completed = self.run_cli(*arguments)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn("Traceback", completed.stderr)
                self.assertNotIn("Audit saved", completed.stderr)
        self.assertFalse(list(self.audits.glob("*.json")))

    def test_input_errors_do_not_publish_success_or_audit(self):
        failures = [
            ("--quarter", ""),
            ("--quarter", "2026Q9"),
            ("--quarter", "2026Q3", "--as-of", ""),
            ("--quarter", "2026Q3", "--explain", ""),
            ("--quarter", "2026Q3", "--as-of", "2026-02-30"),
            ("--quarter", "2026Q3", "--as-of", "2026-09-29"),
            ("--quarter", "2026Q3", "--explain", "Unknown Company"),
            ("--quarter", "2026Q3", "--data-dir", self.work / "absent"),
            ("--quarter", "2026Q3", "--explain", "Pinewell", "--format", "json"),
        ]
        for args in failures:
            with self.subTest(args=args):
                completed = self.run_cli(*args)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertNotIn("Traceback", completed.stderr)
                self.assertNotIn("Audit saved", completed.stderr)
        self.assertFalse(list(self.audits.glob("*.json")))

    def test_audit_destination_failure_prevents_report(self):
        blocker = self.work / "not-a-directory"
        blocker.write_text("preserve this file")
        completed = self.run_cli("--quarter", "2026Q3", audit_dir=blocker)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("AUDIT_ERROR", completed.stderr)
        self.assertEqual(blocker.read_text(), "preserve this file")

    def test_tampered_receipt_is_rejected_without_output(self):
        original = self.success_json("--quarter", "2026Q3")
        path = Path(original["audit_file"])
        receipt = json.loads(path.read_text())
        receipt["report"]["companies"][0]["rounded_fund_position_value"] = "1"
        path.write_text(json.dumps(receipt))
        completed = self.run_cli("--replay", path)
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertIn("AUDIT_ERROR", completed.stderr)
        self.assertEqual(len(list(self.audits.glob("*.json"))), 1)

    def test_success_does_not_modify_original_data_or_allocator(self):
        paths = [*sorted((ROOT / "data").iterdir()), ROOT / "waterfall.py"]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.success_json("--quarter", "2026Q3")
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
