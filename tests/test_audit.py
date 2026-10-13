"""Receipts must preserve evidence, fail honestly and replay without source CSVs."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest import mock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quarterly_marks import audit
from quarterly_marks.domain import AuditError, Quarter
from quarterly_marks.inputs import load_dataset
from quarterly_marks.valuation import build_report


CANDIDATE = Path(__file__).resolve().parents[1]


def reseal(receipt: dict) -> dict:
    """Independent canonical digest for adversarial, internally consistent test files."""
    unsigned = {key: value for key, value in receipt.items() if key != "integrity_sha256"}
    receipt["integrity_sha256"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")).hexdigest()
    return receipt


class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(CANDIDATE / "data")
        cls.report = build_report(cls.dataset, Quarter.parse("2026Q3"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.receipt = audit.create_receipt(self.dataset, self.report)

    def raw_receipt(self, receipt: dict) -> Path:
        path = self.directory / "input.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        return path

    def test_round_trip_preserves_exact_sources_and_report(self):
        path = audit.write_receipt(self.receipt, self.directory / "nested" / "audit")
        saved = audit.load_receipt(path)
        self.assertEqual(saved, self.receipt)
        self.assertEqual(saved["report"], self.report)
        self.assertEqual({name: source["text"] for name, source in saved["sources"].items()},
                         dict(self.dataset.sources))
        self.assertEqual(str(UUID(saved["run_id"])), saved["run_id"])
        self.assertTrue(saved["created_at"].endswith("+00:00"))
        self.assertEqual(set(saved["engine"]["code_sha256"]), set(audit.CODE_FILES))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_each_run_has_new_identity_but_same_financial_report(self):
        second = audit.create_receipt(self.dataset, self.report)
        self.assertNotEqual(second["run_id"], self.receipt["run_id"])
        self.assertEqual(second["report"], self.receipt["report"])

    def test_receipt_does_not_alias_mutable_caller_report(self):
        report = copy.deepcopy(self.report)
        receipt = audit.create_receipt(self.dataset, report)
        report["companies"][0]["arr"] = "1"
        self.assertNotEqual(receipt["report"]["companies"][0]["arr"], "1")

    def test_replay_uses_snapshots_after_input_directory_is_removed(self):
        inputs = self.directory / "sources"
        inputs.mkdir()
        for name, text in self.dataset.sources.items():
            (inputs / name).write_bytes(text.encode("utf-8"))
        dataset = load_dataset(inputs)
        report = build_report(dataset, Quarter.parse("2026Q3"))
        path = audit.write_receipt(audit.create_receipt(dataset, report), self.directory / "audit")
        for source in inputs.iterdir():
            source.write_text("changed after valuation", encoding="utf-8")
            source.unlink()
        inputs.rmdir()
        with mock.patch("quarterly_marks.inputs.load_dataset", side_effect=AssertionError("current sources read")):
            saved, replayed = audit.replay_receipt(path)
        self.assertEqual(saved["report"], replayed)
        self.assertEqual(replayed, report)

    def test_historical_replay_and_parent_lineage(self):
        report = build_report(self.dataset, Quarter.parse("2026Q2"), date(2026, 8, 14))
        original = audit.create_receipt(self.dataset, report)
        path = audit.write_receipt(original, self.directory)
        saved, reproduced = audit.replay_receipt(path)
        replay = audit.create_receipt(self.dataset, reproduced, operation="replay",
                                      parent_run_ids=[saved["run_id"]])
        self.assertEqual(replay["parent_run_ids"], [original["run_id"]])
        self.assertEqual(replay["report"], original["report"])
        self.assertNotEqual(replay["run_id"], original["run_id"])

    def test_minor_and_patch_runtime_differences_require_exact_recomputation(self):
        major, minor, patch = self.receipt["engine"]["runtime"]["python"].split(".")
        for version in (f"{major}.{int(minor) + 1}.{patch}", f"{major}.{minor}.{int(patch) + 1}"):
            with self.subTest(recorded_python=version):
                changed = copy.deepcopy(self.receipt)
                changed["engine"]["runtime"]["python"] = version
                path = self.raw_receipt(reseal(changed))
                original_bytes = path.read_bytes()
                with mock.patch("quarterly_marks.valuation.build_report", wraps=build_report) as calculate:
                    saved, report = audit.replay_receipt(path)
                calculate.assert_called_once()
                self.assertEqual(saved, changed)
                self.assertEqual(report, self.report)
                self.assertEqual(path.read_bytes(), original_bytes)

    def test_runtime_difference_does_not_allow_financial_or_warning_mismatches(self):
        major, minor, patch = self.receipt["engine"]["runtime"]["python"].split(".")
        for field in ("arr", "warnings"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.receipt)
                changed["engine"]["runtime"]["python"] = f"{major}.{int(minor) + 1}.{patch}"
                if field == "arr":
                    changed["report"]["companies"][0][field] = "1"
                else:
                    changed["report"]["companies"][0][field].append(
                        {"code": "REVIEW", "message": "A warning not produced by these inputs."})
                with self.assertRaisesRegex(AuditError, rf"replay result mismatch at report\.companies\[0\]\.{field}"):
                    audit.replay_receipt(self.raw_receipt(reseal(changed)))

    def test_runtime_difference_does_not_bypass_engine_code_or_source_integrity(self):
        major, minor, patch = self.receipt["engine"]["runtime"]["python"].split(".")
        for field, message in (("version", "incompatible engine version"),
                               ("code", "calculation code has changed"),
                               ("source", "source hash mismatch")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.receipt)
                changed["engine"]["runtime"]["python"] = f"{major}.{int(minor) + 1}.{patch}"
                if field == "version":
                    changed["engine"]["version"] = "future-version"
                elif field == "code":
                    changed["engine"]["code_sha256"]["waterfall.py"] = "0" * 64
                else:
                    changed["sources"]["kpis.csv"]["text"] += "\n"
                with self.assertRaisesRegex(AuditError, message):
                    audit.replay_receipt(self.raw_receipt(reseal(changed)))

    def test_changed_report_without_new_hash_is_rejected(self):
        changed = copy.deepcopy(self.receipt)
        changed["report"]["companies"][0]["arr"] = "1"
        with self.assertRaisesRegex(AuditError, "integrity hash mismatch"):
            audit.load_receipt(self.raw_receipt(changed))

    def test_source_hash_is_checked_independently_of_envelope_hash(self):
        changed = copy.deepcopy(self.receipt)
        changed["sources"]["kpis.csv"]["text"] += "\n"
        reseal(changed)
        with self.assertRaisesRegex(AuditError, "source hash mismatch: kpis.csv"):
            audit.load_receipt(self.raw_receipt(changed))

    def test_replay_catches_resealed_false_result_with_field_path(self):
        changed = copy.deepcopy(self.receipt)
        changed["report"]["companies"][0]["arr"] = "1"
        path = self.raw_receipt(reseal(changed))
        audit.load_receipt(path)  # Integrity is not proof that arithmetic is correct.
        with self.assertRaisesRegex(AuditError, r"replay result mismatch at report\.companies\[0\]\.arr"):
            audit.replay_receipt(path)

    def test_replay_catches_resealed_changed_input(self):
        changed = copy.deepcopy(self.receipt)
        source = changed["sources"]["kpis.csv"]
        source["text"] = source["text"].replace("2026Q3,21000000", "2026Q3,22000000")
        source["sha256"] = hashlib.sha256(source["text"].encode()).hexdigest()
        with self.assertRaisesRegex(AuditError, "replay result mismatch"):
            audit.replay_receipt(self.raw_receipt(reseal(changed)))

    def test_request_must_match_report(self):
        changed = copy.deepcopy(self.receipt)
        changed["request"]["as_of"] = "2026-10-20"
        with self.assertRaisesRegex(AuditError, "does not match"):
            audit.load_receipt(self.raw_receipt(reseal(changed)))

    def test_unknown_schema_and_engine_versions_are_rejected(self):
        changed = copy.deepcopy(self.receipt)
        changed["schema_version"] = 999
        with self.assertRaisesRegex(AuditError, "unsupported audit schema"):
            audit.load_receipt(self.raw_receipt(reseal(changed)))
        changed = copy.deepcopy(self.receipt)
        changed["engine"]["version"] = "future-version"
        path = self.raw_receipt(reseal(changed))
        with self.assertRaisesRegex(AuditError, "incompatible engine version"):
            audit.load_receipt(path)
        self.assertEqual(audit.load_receipt(path, require_compatible=False), changed)

    def test_changed_calculation_code_is_rejected_but_comparable(self):
        changed = copy.deepcopy(self.receipt)
        changed["engine"]["code_sha256"]["waterfall.py"] = "0" * 64
        path = self.raw_receipt(reseal(changed))
        with self.assertRaisesRegex(AuditError, "calculation code has changed.*waterfall.py"):
            audit.replay_receipt(path)
        self.assertEqual(audit.load_receipt(path, require_compatible=False), changed)

    def test_duplicate_json_keys_and_nonfinite_json_are_rejected(self):
        path = self.directory / "malformed.json"
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(AuditError, "duplicate key"):
            audit.load_receipt(path)
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                path.write_text('{"value":' + token + '}', encoding="utf-8")
                with self.assertRaisesRegex(AuditError, "non-finite"):
                    audit.load_receipt(path)
        text = json.dumps(self.receipt).replace('"companies":', '"overflow":1e309,"companies":', 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(AuditError, "finite"):
            audit.load_receipt(path)

    def test_invalid_utf8_and_oversized_files_are_rejected(self):
        path = self.directory / "invalid.json"
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(AuditError, "invalid audit JSON"):
            audit.load_receipt(path)
        path = self.raw_receipt(self.receipt)
        with mock.patch.object(audit, "MAX_RECEIPT_BYTES", 10):
            with self.assertRaisesRegex(AuditError, "exceeds"):
                audit.load_receipt(path)

    def test_invalid_envelope_shapes_fail_actionably(self):
        mutations = {
            "bool schema": lambda r: r.update(schema_version=True),
            "bad uuid": lambda r: r.update(run_id="not-a-uuid"),
            "naive timestamp": lambda r: r.update(created_at="2026-10-20T12:00:00"),
            "unknown field": lambda r: r.update(unexpected="value"),
            "missing source": lambda r: r["sources"].pop("kpis.csv"),
            "source text type": lambda r: r["sources"]["kpis.csv"].update(text=1),
            "duplicate company": lambda r: r["report"]["companies"].append(r["report"]["companies"][0]),
            "empty portfolio": lambda r: r["report"].update(companies=[]),
            "wrong quarter end": lambda r: r["report"].update(quarter_end="2026-06-30"),
            "wrong cutoff mode": lambda r: r["report"].update(cutoff_mode="as_of"),
            "missing monetary field": lambda r: r["report"]["companies"][0].pop("arr"),
            "numeric JSON money": lambda r: r["report"]["companies"][0].update(arr=1),
            "nonfinite money string": lambda r: r["report"]["companies"][0].update(arr="NaN"),
            "unbounded money text": lambda r: r["report"]["companies"][0].update(arr="1" * 129),
            "invalid ownership": lambda r: r["report"]["companies"][0].update(ownership_pct="101"),
            "fractional published dollar": lambda r: r["report"]["companies"][0].update(prior_quarter_mark="1.5"),
            "unexplained missing growth": lambda r: r["report"]["companies"][0].update(arr_growth_pct=None, arr_growth_reason=None),
            "bad warnings": lambda r: r["report"]["companies"][0].update(warnings=[{"code": "REVIEW"}]),
            "replay without parent": lambda r: r.update(operation="replay"),
            "self parent": lambda r: r.update(operation="replay", parent_run_ids=[r["run_id"]]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(self.receipt)
                mutate(changed)
                with self.assertRaises(AuditError):
                    audit.load_receipt(self.raw_receipt(reseal(changed)))

    def test_write_never_overwrites_existing_receipt(self):
        path = audit.write_receipt(self.receipt, self.directory)
        original = path.read_bytes()
        changed = copy.deepcopy(self.receipt)
        changed["created_at"] = "2026-10-21T00:00:00+00:00"
        with self.assertRaisesRegex(AuditError, "cannot publish audit"):
            audit.write_receipt(reseal(changed), self.directory)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_fsync_failure_leaves_no_published_or_temporary_files(self):
        with mock.patch.object(audit.os, "fsync", side_effect=OSError(errno.EIO, "simulated I/O failure")):
            with self.assertRaisesRegex(AuditError, "cannot publish audit"):
                audit.write_receipt(self.receipt, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_link_failure_leaves_no_partial_receipt(self):
        with mock.patch.object(audit.os, "link", side_effect=OSError(errno.EIO, "link failed")):
            with self.assertRaisesRegex(AuditError, "cannot publish audit"):
                audit.write_receipt(self.receipt, self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_readback_failure_is_not_reported_as_success(self):
        with mock.patch.object(audit, "load_receipt", side_effect=AuditError("read-back failed")):
            with self.assertRaisesRegex(AuditError, "read-back failed"):
                audit.write_receipt(self.receipt, self.directory)
        published = list(self.directory.glob("*.json"))
        self.assertEqual(len(published), 1)
        self.assertEqual(audit.load_receipt(published[0]), self.receipt)
        self.assertEqual(list(self.directory.iterdir()), published)

    def test_invalid_destination_is_actionable(self):
        destination = self.directory / "a-file"
        destination.write_text("keep this", encoding="utf-8")
        with self.assertRaisesRegex(AuditError, "cannot publish audit"):
            audit.write_receipt(self.receipt, destination)
        self.assertEqual(destination.read_text(), "keep this")

    def test_two_concurrent_runs_publish_distinct_complete_files(self):
        receipts = [self.receipt, audit.create_receipt(self.dataset, self.report)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(lambda receipt: audit.write_receipt(receipt, self.directory), receipts))
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual([audit.load_receipt(path) for path in paths], receipts)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_decimal_formatting_only_does_not_become_financial_change(self):
        changed = copy.deepcopy(self.receipt)
        row = changed["report"]["companies"][0]
        row["arr"] += ".0"
        row["selected_inputs"]["arr"]["value"] += ".0"
        comparison = audit.compare_receipts(self.receipt, reseal(changed))
        self.assertFalse(comparison["has_changes"])
        self.assertEqual(comparison["changes"], [])

    def test_raw_source_cell_text_changes_remain_visible(self):
        changed = copy.deepcopy(self.receipt)
        changed["report"]["companies"][0]["selected_inputs"]["arr"]["source"]["raw"]["arr"] += ".0"
        comparison = audit.compare_receipts(self.receipt, reseal(changed))
        self.assertEqual(comparison["changed_company_count"], 1)
        change = comparison["changes"][0]
        self.assertEqual(change["financial_changes"], [])
        self.assertEqual(change["selected_input_changes"][0]["field"], "selected_inputs.arr.source.raw.arr")

    def test_comparison_handles_added_removed_companies(self):
        changed = copy.deepcopy(self.receipt)
        removed = changed["report"]["companies"][0]["company"]
        changed["report"]["companies"][0]["company"] = "New company"
        comparison = audit.compare_receipts(self.receipt, reseal(changed))
        self.assertEqual(comparison["added_companies"], ["New company"])
        self.assertEqual(comparison["removed_companies"], [removed])

    def test_comparison_preserves_warning_changes_separately(self):
        changed = copy.deepcopy(self.receipt)
        changed["report"]["companies"][0]["warnings"].append({"code": "REVIEW", "message": "Review input."})
        comparison = audit.compare_receipts(self.receipt, reseal(changed))
        self.assertTrue(comparison["changes"][0]["warning_changes"])
        self.assertFalse(comparison["changes"][0]["financial_changes"])

    def test_comparison_receipt_is_self_contained_and_replayable(self):
        earlier = build_report(self.dataset, Quarter.parse("2026Q3"), date(2026, 10, 16))
        first = audit.create_receipt(self.dataset, earlier)
        comparison = audit.compare_receipts(first, self.receipt)
        changed_names = [change["company"] for change in comparison["changes"]]
        self.assertEqual(changed_names, ["Kestrel Labs"])
        receipt = audit.create_comparison_receipt(first, self.receipt, comparison)
        path = audit.write_receipt(receipt, self.directory)
        with mock.patch("quarterly_marks.valuation.build_report", side_effect=AssertionError("comparison must not revalue")):
            saved, replayed = audit.replay_receipt(path)
        self.assertEqual(saved, receipt)
        self.assertEqual(replayed, comparison)
        self.assertEqual(receipt["parent_run_ids"], [first["run_id"], self.receipt["run_id"]])

    def test_comparison_accepts_old_financial_engines_without_revaluing(self):
        old = copy.deepcopy(self.receipt)
        old["engine"]["version"] = "historical-engine"
        old["engine"]["code_sha256"]["waterfall.py"] = "0" * 64
        old["engine"]["runtime"] = {"implementation": "OtherPython", "python": "99.0.0"}
        reseal(old)
        comparison = audit.compare_receipts(old, self.receipt)
        self.assertTrue(comparison["has_changes"])
        self.assertEqual(len(comparison["engine_changes"]), 4)
        self.assertEqual(comparison["changed_company_count"], 0)
        receipt = audit.create_comparison_receipt(old, self.receipt, comparison)
        path = audit.write_receipt(receipt, self.directory)
        with mock.patch("quarterly_marks.valuation.build_report", side_effect=AssertionError("comparison must not revalue")):
            self.assertEqual(audit.replay_receipt(path)[1], comparison)

    def test_comparison_result_and_parent_ids_are_verified(self):
        second = audit.create_receipt(self.dataset, self.report)
        comparison = audit.compare_receipts(self.receipt, second)
        receipt = audit.create_comparison_receipt(self.receipt, second, comparison)
        changed = copy.deepcopy(receipt)
        changed["comparison"]["has_changes"] = True
        with self.assertRaisesRegex(AuditError, "comparison result does not match"):
            audit.load_receipt(self.raw_receipt(reseal(changed)))
        changed = copy.deepcopy(receipt)
        changed["parent_run_ids"].reverse()
        with self.assertRaisesRegex(AuditError, "comparison parent IDs"):
            audit.load_receipt(self.raw_receipt(reseal(changed)))

    def test_comparison_receipts_cannot_be_nested(self):
        comparison = audit.compare_receipts(self.receipt, self.receipt)
        parent = audit.create_comparison_receipt(self.receipt, self.receipt, comparison)
        with self.assertRaisesRegex(AuditError, "nested comparisons"):
            audit.create_comparison_receipt(parent, self.receipt, {})


if __name__ == "__main__":
    unittest.main()
