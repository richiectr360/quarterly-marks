"""Input contracts tested independently of the valuation implementation."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

from quarterly_marks.domain import MarksError, Quarter, parse_date
from quarterly_marks.inputs import MAX_SOURCE_BYTES, load_dataset, parse_sources


def sample_sources() -> dict[str, str]:
    return {
        "kpis.csv": "company,quarter,arr,submitted_at\nAcme,2026Q2,100.25,2026-07-01\n",
        "valuation_inputs.csv": "company,quarter,ev_arr_multiple,entered_by,entered_at,note\nAcme,2026Q2,5.5,analyst,2026-07-02,\n",
        "q2_marks.csv": "company,quarter,fund_position_value,reported_to_lps_on\nAcme,2026Q2,50,2026-08-14\n",
        "cap_tables.json": json.dumps({"Acme": {
            "common_shares": 100,
            "series": [{"name": "Series A", "shares": 10, "issue_price": "2.00",
                        "issue_date": "2020-01-01", "seniority": 1, "fund_shares": 5}],
        }}, indent=2),
    }


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = sample_sources()

    def rejected(self, filename: str, text: str, message: str = "") -> None:
        sources = {**self.sources, filename: text}
        with self.assertRaises(MarksError) as raised:
            parse_sources(sources)
        self.assertIn(filename, str(raised.exception))
        if message:
            self.assertIn(message, str(raised.exception))

    def cap_change(self, field: str, value: object, *, company: bool = False) -> str:
        caps = json.loads(self.sources["cap_tables.json"])
        target = caps["Acme"] if company else caps["Acme"]["series"][0]
        target[field] = value
        return json.dumps(caps)

    def test_values_are_decimal_and_preserve_source_precision(self) -> None:
        with localcontext() as context:
            context.prec = 2
            dataset = parse_sources(self.sources)
        self.assertEqual(dataset.kpis[0].value, Decimal("100.25"))
        self.assertEqual(dataset.multiples[0].value, Decimal("5.5"))
        self.assertEqual(dataset.cap_tables["Acme"].series[0].issue_price, Decimal("2.00"))
        self.assertEqual(dataset.kpis[0].quarter, Quarter(2026, 2))
        self.assertEqual(dataset.kpis[0].known_on, date(2026, 7, 1))

    def test_raw_cells_and_multiline_record_locations_are_preserved(self) -> None:
        self.sources["valuation_inputs.csv"] = (
            "company,quarter,ev_arr_multiple,entered_by,entered_at,note\r\n"
            ' Acme , 2026Q2 , 5.50 , analyst , 2026-07-02 ,"first line\r\nsecond, line"\r\n'
            "Acme,2026Q2,6,analyst,2026-07-03,\r\n"
        )
        observations = parse_sources(self.sources).multiples
        self.assertEqual([row.source.row_number for row in observations], [2, 4])
        first = observations[0]
        self.assertEqual(first.company, "Acme")
        self.assertEqual(first.value, Decimal("5.50"))
        self.assertEqual(dict(first.source.cells)["company"], " Acme ")
        self.assertEqual(dict(first.source.cells)["ev_arr_multiple"], " 5.50 ")
        self.assertEqual(dict(first.source.cells)["note"], "first line\r\nsecond, line")

    def test_snapshots_are_lossless_and_independent_of_caller(self) -> None:
        original = {name: "\ufeff" + text.replace("\n", "\r\n") for name, text in self.sources.items()}
        sources = dict(original)
        dataset = parse_sources(sources)
        self.assertEqual(sources, original)
        self.assertEqual(dict(dataset.sources), original)
        sources["kpis.csv"] = "replaced by caller"
        self.assertEqual(dict(dataset.sources), original)
        with self.assertRaises(TypeError):
            dataset.sources["kpis.csv"] = "cannot overwrite history"

    def test_disk_loader_preserves_exact_utf8_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = {}
            for name, text in self.sources.items():
                expected[name] = ("\ufeff" + text.replace("\n", "\r\n")).encode("utf-8")
                (Path(directory) / name).write_bytes(expected[name])
            dataset = load_dataset(Path(directory))
            for name, raw in expected.items():
                self.assertEqual(dataset.sources[name].encode("utf-8"), raw)
                self.assertEqual((Path(directory) / name).read_bytes(), raw)

    def test_exact_file_set_is_required(self) -> None:
        for change in ("missing", "extra"):
            with self.subTest(change=change):
                sources = dict(self.sources)
                if change == "missing":
                    del sources["kpis.csv"]
                else:
                    sources["extra.csv"] = ""
                with self.assertRaises(MarksError):
                    parse_sources(sources)

    def test_optional_manifest_and_editable_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in self.sources.items():
                (root / name).write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(MarksError, "manifest is missing"):
                load_dataset(root, require_manifest=True)
            self.assertEqual(load_dataset(root).kpis[0].value, Decimal("100.25"))
            path = root / "kpis.csv"
            path.write_text(path.read_text().replace("100.25", "200"), encoding="utf-8")
            self.assertEqual(load_dataset(root).kpis[0].value, Decimal("200"))

    def test_present_manifest_is_verified_even_without_strict_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {}
            for name, text in self.sources.items():
                raw = text.encode("utf-8")
                (root / name).write_bytes(raw)
                sources[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            body = {"schema_version": 1, "sources": sources}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            manifest = root / "batch-manifest.json"
            manifest.write_text(json.dumps({**body, "batch_id": digest}), encoding="utf-8")
            self.assertEqual(load_dataset(root, require_manifest=True).kpis[0].value, Decimal("100.25"))
            (root / "kpis.csv").write_text(self.sources["kpis.csv"].replace("100.25", "200"), encoding="utf-8")
            with self.assertRaisesRegex(MarksError, "source bytes do not match"):
                load_dataset(root)
            manifest.write_text('{"schema_version": 999}', encoding="utf-8")
            with self.assertRaises(MarksError):
                load_dataset(root)

    def test_header_errors_are_actionable(self) -> None:
        for header in ("company,quarter,arr,arr", "company,quarter,arr",
                       "company,quarter,arr,submitted_at,extra"):
            with self.subTest(header=header):
                self.rejected("kpis.csv", header + "\nAcme,2026Q2,100,2026-07-01\n", "row 1")

    def test_reordered_headers_are_valid(self) -> None:
        self.sources["kpis.csv"] = "arr,submitted_at,company,quarter\n100.25,2026-07-01,Acme,2026Q2\n"
        self.assertEqual(parse_sources(self.sources).kpis[0].value, Decimal("100.25"))

    def test_malformed_row_width_and_quoting_are_rejected(self) -> None:
        header = self.sources["kpis.csv"].splitlines()[0] + "\n"
        for row in ("Acme,2026Q2,100", "Acme,2026Q2,100,2026-07-01,extra", '"Acme,2026Q2,100,2026-07-01'):
            with self.subTest(row=row):
                self.rejected("kpis.csv", header + row + "\n", "row 2")

    def test_unknown_company_and_blank_analyst_are_rejected(self) -> None:
        self.rejected("kpis.csv", self.sources["kpis.csv"].replace("Acme", "Unknown"), "unknown company")
        self.rejected("valuation_inputs.csv", self.sources["valuation_inputs.csv"].replace(",analyst,", ",  ,"), "entered_by")

    def test_invalid_dates_and_quarters_are_rejected(self) -> None:
        for bad in ("2026-02-30", "2026-7-01", "2026-07-01T00:00:00", "not-a-date"):
            with self.subTest(date=bad):
                self.rejected("kpis.csv", self.sources["kpis.csv"].replace("2026-07-01", bad), "row 2")
        for bad in ("2026Q0", "2026Q5", "2026-Q2", "26Q2", "0000Q2"):
            with self.subTest(quarter=bad):
                self.rejected("kpis.csv", self.sources["kpis.csv"].replace("2026Q2", bad), "quarter")

    def test_quarter_boundaries_include_leap_year_and_earliest_supported_year(self) -> None:
        self.assertEqual(Quarter.parse("2024Q1").end, date(2024, 3, 31))
        self.assertEqual(Quarter.parse("2026Q1").previous, Quarter(2025, 4))
        self.assertEqual(parse_date("2024-02-29"), date(2024, 2, 29))
        self.assertIsNone(Quarter.parse("0001Q1").previous)

    def test_reporting_dates_cannot_precede_period_but_multiples_can(self) -> None:
        self.rejected("kpis.csv", self.sources["kpis.csv"].replace("2026-07-01", "2026-06-29"), "quarter-end")
        self.rejected("q2_marks.csv", self.sources["q2_marks.csv"].replace("2026-08-14", "2026-06-29"), "quarter-end")
        self.sources["valuation_inputs.csv"] = self.sources["valuation_inputs.csv"].replace("2026-07-02", "2026-06-01")
        self.assertEqual(parse_sources(self.sources).multiples[0].known_on, date(2026, 6, 1))

    def test_nonfinite_negative_and_excessive_values_are_rejected(self) -> None:
        invalid = ("NaN", "Infinity", "-Infinity", "-1", "1e25", "0.0000000000001",
                   "1000000000000000000000000.000000000000", "1e999999999999999999999999999999", "1_000")
        for value in invalid:
            with self.subTest(value=value):
                self.rejected("kpis.csv", self.sources["kpis.csv"].replace("100.25", value), "row 2")

    def test_zero_and_numeric_boundaries_are_valid_without_signed_zero(self) -> None:
        for value in ("0", "-0.00", "1e24", "0.000000000001", "999999999999999999999999.123456789012"):
            with self.subTest(value=value):
                sources = {**self.sources, "kpis.csv": self.sources["kpis.csv"].replace("100.25", value)}
                parsed = parse_sources(sources).kpis[0].value
                self.assertEqual(parsed, Decimal(value))
                if parsed == 0:
                    self.assertFalse(parsed.is_signed())
        self.sources["valuation_inputs.csv"] = self.sources["valuation_inputs.csv"].replace("5.5", "0")
        self.assertEqual(parse_sources(self.sources).multiples[0].value, Decimal(0))

    def test_reported_marks_require_whole_dollars_but_are_not_q2_only(self) -> None:
        self.rejected("q2_marks.csv", self.sources["q2_marks.csv"].replace(",50,", ",50.01,"), "whole-dollar")
        self.sources["q2_marks.csv"] = self.sources["q2_marks.csv"].replace("2026Q2", "2026Q1")
        self.assertEqual(parse_sources(self.sources).reported[0].quarter, Quarter(2026, 1))

    def test_revisions_and_exact_duplicates_are_not_resolved_by_parser(self) -> None:
        self.sources["kpis.csv"] += (
            "Acme,2026Q2,95,2026-09-01\n"
            "Acme,2026Q2,100.25,2026-07-01\n"
            "Acme,2026Q2,96,2026-09-01\n"
        )
        values = [row.value for row in parse_sources(self.sources).kpis]
        self.assertEqual(values, list(map(Decimal, ("100.25", "95", "100.25", "96"))))

    def test_json_duplicate_keys_constants_and_malformed_documents_are_rejected(self) -> None:
        self.rejected("cap_tables.json", '{"Acme": {}, "Acme": {}}', "duplicate")
        self.rejected("cap_tables.json", self.sources["cap_tables.json"].replace('"shares": 10', '"shares": 10, "shares": 11'), "duplicate")
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                self.rejected("cap_tables.json", self.sources["cap_tables.json"].replace('"2.00"', value), "constant")
        for text in ("{", "[]", "{}", "null"):
            with self.subTest(text=text):
                self.rejected("cap_tables.json", text)

    def test_cap_tables_reject_unknown_and_missing_fields(self) -> None:
        self.rejected("cap_tables.json", self.cap_change("unknown", 1), "unknown fields")
        caps = json.loads(self.sources["cap_tables.json"])
        del caps["Acme"]["series"][0]["issue_price"]
        self.rejected("cap_tables.json", json.dumps(caps), "missing fields")

    def test_share_and_price_constraints(self) -> None:
        for field, value in (("shares", 0), ("shares", -1), ("shares", True), ("shares", 1.5),
                             ("shares", "10"), ("shares", 10**18 + 1), ("fund_shares", 11),
                             ("fund_shares", -1), ("seniority", True), ("seniority", -1),
                             ("issue_price", "0"), ("issue_price", "-1"), ("issue_price", True)):
            with self.subTest(field=field, value=value):
                self.rejected("cap_tables.json", self.cap_change(field, value), field)
        self.rejected("cap_tables.json", self.cap_change("common_shares", True, company=True), "common_shares")

    def test_series_names_and_size_are_validated_and_order_is_deterministic(self) -> None:
        self.rejected("cap_tables.json", self.cap_change("name", "Common"), "reserved")
        caps = json.loads(self.sources["cap_tables.json"])
        first = caps["Acme"]["series"][0]
        caps["Acme"]["series"].append(dict(first))
        self.rejected("cap_tables.json", json.dumps(caps), "duplicate")
        caps["Acme"]["series"] = [{**first, "name": "Series Z"}, first]
        self.sources["cap_tables.json"] = json.dumps(caps)
        self.assertEqual([row.name for row in parse_sources(self.sources).cap_tables["Acme"].series], ["Series A", "Series Z"])
        caps["Acme"]["series"] = [{**first, "name": str(index)} for index in range(33)]
        self.rejected("cap_tables.json", json.dumps(caps), "at most 32")

    def test_zero_outstanding_is_rejected_but_common_only_is_valid(self) -> None:
        self.rejected("cap_tables.json", '{"Acme":{"common_shares":0,"series":[]}}', "positive")
        self.sources["cap_tables.json"] = '{"Acme":{"common_shares":100,"series":[]}}'
        self.assertEqual(parse_sources(self.sources).cap_tables["Acme"].series, ())

    def test_labels_reject_controls_and_escaped_surrogates(self) -> None:
        for name in ("A\x1b[31m", "A\nB", "A\u202eB", "\ud800", "\udfff"):
            with self.subTest(name=repr(name)):
                self.rejected("cap_tables.json", self.cap_change("name", name))
        self.rejected("valuation_inputs.csv", self.sources["valuation_inputs.csv"].replace("analyst", "analyst\x00"), "entered_by")

    def test_missing_invalid_utf8_and_oversized_files_fail_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(MarksError, "kpis.csv.*cannot read"):
                load_dataset(root)
            for name, text in self.sources.items():
                (root / name).write_bytes(text.encode("utf-8"))
            (root / "kpis.csv").write_bytes(b"\xff")
            with self.assertRaisesRegex(MarksError, "kpis.csv.*invalid UTF-8"):
                load_dataset(root)
            (root / "kpis.csv").write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
            with self.assertRaisesRegex(MarksError, "kpis.csv.*input limit"):
                load_dataset(root)
        self.rejected("kpis.csv", "x" * (MAX_SOURCE_BYTES + 1), "input limit")
        self.rejected("kpis.csv", "\ud800", "invalid UTF-8")


if __name__ == "__main__":
    unittest.main()
