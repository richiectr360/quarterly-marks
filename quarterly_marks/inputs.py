"""Strict, lossless loading of the exercise's four source files.

Financial values are bounded to 10**24, 36 coefficient digits and 12
fractional places. These limits keep malformed/excessive inputs out of the
calculation; they never silently round a source value.
"""


from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import stat
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .domain import CapTable, Dataset, MarksError, Observation, Quarter, Series, SourceRow, parse_date


SOURCE_FILENAMES = ("kpis.csv", "valuation_inputs.csv", "q2_marks.csv", "cap_tables.json")
MANIFEST_FILENAME = "batch-manifest.json"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SHARES = 10**18
MAX_VALUE = Decimal("1e24")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_SCHEMAS = {
    "kpis.csv": ("company", "quarter", "arr", "submitted_at"),
    "valuation_inputs.csv": ("company", "quarter", "ev_arr_multiple", "entered_by", "entered_at", "note"),
    "q2_marks.csv": ("company", "quarter", "fund_position_value", "reported_to_lps_on"),
}

#_label()Checks text fields. Rejects prohibited characters and disallowed blanks.
def _label(value: object, where: str, *, blank: bool = False, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise MarksError(f"{where}: expected text")
    allowed_controls = "\n\r\t" if multiline else ""
    if any(unicodedata.category(c) == "Cs" or
           (unicodedata.category(c) in {"Cc", "Cf"} and c not in allowed_controls) for c in value):
        raise MarksError(f"{where}: control, formatting, and surrogate characters are not allowed")
    result = value.strip()
    if not result and not blank:
        raise MarksError(f"{where}: must not be blank")
    return result

#_decimal()Converts a number to Decimal; checks whether its sign, size, precision, and whole-dollar requirement are valid.
def _decimal(value: object, where: str, *, positive: bool = False, whole: bool = False,
             signed: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MarksError(f"{where}: expected a decimal number")
    text = str(value).strip()
    if len(text) > 128 or not _NUMBER.fullmatch(text):
        raise MarksError(f"{where}: expected a finite decimal number")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise MarksError(f"{where}: invalid decimal number") from exc
    if not result.is_finite():
        raise MarksError(f"{where}: expected a finite decimal number")
    if (not signed and result < 0) or (positive and result <= 0) or result.copy_abs() > MAX_VALUE:
        lower = "greater than zero" if positive else "nonnegative"
        bounds = "between -1e24 and 1e24" if signed else f"{lower} and at most 1e24"
        raise MarksError(f"{where}: must be {bounds}")
    parts = result.as_tuple()
    if len(parts.digits) > 36 or parts.exponent < -12:
        raise MarksError(f"{where}: at most 36 significant digits and 12 fractional places are supported")
    if whole and result != result.to_integral_value():
        raise MarksError(f"{where}: expected a whole-dollar amount")
    # Decimal preserves a sign on zero; it is not meaningful for these inputs.
    return Decimal(0) if result == 0 else result

#_integer()Checks integer fields such as shares. Rejects fractions, booleans, and out-of-range values.
def _integer(value: object, where: str, *, positive: bool = False) -> int:
    # JSON shares must be integers, not booleans, fractions, or numeric strings.
    if type(value) is not int or value < (1 if positive else 0) or value > MAX_SHARES:
        raise MarksError(f"{where}: expected an integer from {1 if positive else 0} to {MAX_SHARES}")
    return value

#_fields()Checks a JSON object for missing or unexpected fields.
def _fields(value: object, expected: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise MarksError(f"{where}: expected an object")
    missing, unknown = expected - value.keys(), value.keys() - expected
    if missing or unknown:
        details = []
        if missing:
            details.append("missing fields " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown fields " + ", ".join(sorted(unknown)))
        raise MarksError(f"{where}: {'; '.join(details)}")
    return value


def _cap_tables(text: str) -> Mapping[str, CapTable]:
    filename = "cap_tables.json"

    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise MarksError(f"{filename}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise MarksError(f"{filename}: invalid JSON numeric constant {value}")

    try:
        raw = json.loads(text.removeprefix("\ufeff"), object_pairs_hook=unique_object,
                         parse_float=Decimal, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise MarksError(f"{filename} line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except (ValueError, InvalidOperation, RecursionError) as exc:
        raise MarksError(f"{filename}: invalid or excessively complex JSON") from exc
    if not isinstance(raw, dict) or not raw:
        raise MarksError(f"{filename}: expected a nonempty company object")
    caps = {}
    for raw_company, value in raw.items():
        company = _label(raw_company, f"{filename} company")
        if company in caps:
            raise MarksError(f"{filename}: duplicate company after trimming {company!r}")
        where = f"{filename} {company!r}"
        value = _fields(value, {"common_shares", "series"}, where)
        common = _integer(value["common_shares"], f"{where}.common_shares")
        if not isinstance(value["series"], list) or len(value["series"]) > 32:
            raise MarksError(f"{where}.series: expected a list with at most 32 series")
        series = []
        names = set()
        for index, item in enumerate(value["series"], 1):
            at = f"{where}.series[{index}]"
            item = _fields(item, {"name", "shares", "issue_price", "issue_date", "seniority", "fund_shares"}, at)
            name = _label(item["name"], f"{at}.name")
            if name == "Common" or name in names:
                raise MarksError(f"{at}.name: reserved or duplicate series name {name!r}")
            names.add(name)
            shares = _integer(item["shares"], f"{at}.shares", positive=True)
            fund_shares = _integer(item["fund_shares"], f"{at}.fund_shares")
            if fund_shares > shares:
                raise MarksError(f"{at}.fund_shares: cannot exceed shares outstanding")
            series.append(Series(
                name=name, shares=shares,
                issue_price=_decimal(item["issue_price"], f"{at}.issue_price", positive=True),
                issue_date=parse_date(_label(item["issue_date"], f"{at}.issue_date"), f"{at}.issue_date"),
                seniority=_integer(item["seniority"], f"{at}.seniority"), fund_shares=fund_shares,
            ))
        if common + sum(s.shares for s in series) == 0:
            raise MarksError(f"{where}: total outstanding shares must be positive")
        caps[company] = CapTable(common, tuple(sorted(series, key=lambda s: s.name)))
    return MappingProxyType(dict(sorted(caps.items())))

#_observations()Reads a CSV, checks its columns and rows, and creates Observation records with source evidence.
def _observations(filename: str, text: str, companies: Mapping[str, CapTable]) -> tuple[Observation, ...]:
    schema = _SCHEMAS[filename]
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    records = []
    try:
        headers = next(reader, None)
        if headers is None:
            raise MarksError(f"{filename} row 1: missing header")
        if len(set(headers)) != len(headers):
            raise MarksError(f"{filename} row 1: duplicate headers")
        if set(headers) != set(schema):
            missing, unknown = set(schema) - set(headers), set(headers) - set(schema)
            raise MarksError(f"{filename} row 1: invalid headers; missing {sorted(missing)}, unknown {sorted(unknown)}")
        while True:
            line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            at = f"{filename} row {line}"
            if len(cells) != len(headers):
                raise MarksError(f"{at}: expected {len(headers)} cells, got {len(cells)}")
            raw = dict(zip(headers, cells))
            company = _label(raw["company"], f"{at} company")
            if company not in companies:
                raise MarksError(f"{at}: unknown company {company!r}")
            try:
                quarter = Quarter.parse(raw["quarter"].strip())
            except MarksError as exc:
                raise MarksError(f"{at}: {exc}") from exc
            value_field = schema[2]
            date_field = "entered_at" if filename == "valuation_inputs.csv" else schema[-1]
            known_on = parse_date(raw[date_field].strip(), f"{at} {date_field}")
            if filename != "valuation_inputs.csv" and known_on < quarter.end:
                raise MarksError(f"{at} {date_field}: cannot precede {quarter} quarter-end {quarter.end}")
            if filename == "valuation_inputs.csv":
                _label(raw["entered_by"], f"{at} entered_by")
                _label(raw["note"], f"{at} note", blank=True, multiline=True)
            value = _decimal(raw[value_field], f"{at} {value_field}", whole=filename == "q2_marks.csv")
            records.append(Observation(company, quarter, value, known_on,
                                       SourceRow(filename, line, tuple(zip(headers, cells)))))
    except csv.Error as exc:
        raise MarksError(f"{filename} row {reader.line_num}: malformed CSV: {exc}") from exc
    return tuple(records)


def parse_sources(sources: Mapping[str, str]) -> Dataset:
    """Parse exact source snapshots without mutating them or resolving revisions."""
    if not isinstance(sources, Mapping):
        raise MarksError("sources: expected a mapping of source filenames to UTF-8 text")
    if set(sources) != set(SOURCE_FILENAMES):
        missing = set(SOURCE_FILENAMES) - set(sources)
        unknown = set(sources) - set(SOURCE_FILENAMES)
        raise MarksError(f"sources: missing {sorted(missing)}, unknown {sorted(unknown, key=str)}")
    snapshots = {}
    for filename in SOURCE_FILENAMES:
        text = sources[filename]
        if not isinstance(text, str):
            raise MarksError(f"{filename}: expected decoded UTF-8 text")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise MarksError(f"{filename}: invalid UTF-8 text") from exc
        if size > MAX_SOURCE_BYTES:
            raise MarksError(f"{filename}: exceeds the {MAX_SOURCE_BYTES}-byte input limit")
        snapshots[filename] = text
    caps = _cap_tables(snapshots["cap_tables.json"])
    return Dataset(
        kpis=_observations("kpis.csv", snapshots["kpis.csv"], caps),
        multiples=_observations("valuation_inputs.csv", snapshots["valuation_inputs.csv"], caps),
        reported=_observations("q2_marks.csv", snapshots["q2_marks.csv"], caps),
        cap_tables=caps, sources=MappingProxyType(snapshots),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _manifest_for_bytes(sources: Mapping[str, bytes]) -> dict:
    body = {"schema_version": MANIFEST_VERSION, "sources": {
        filename: {"sha256": hashlib.sha256(sources[filename]).hexdigest(),
                   "size_bytes": len(sources[filename])}
        for filename in SOURCE_FILENAMES
    }}
    return {**body, "batch_id": hashlib.sha256(_canonical_json(body)).hexdigest()}


def _parse_manifest(data: bytes) -> dict:
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise MarksError(f"{MANIFEST_FILENAME}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise MarksError(f"{MANIFEST_FILENAME}: invalid JSON numeric constant {value}")

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MarksError(f"{MANIFEST_FILENAME}: invalid UTF-8 JSON: {exc}") from exc
    raw = _fields(raw, {"schema_version", "batch_id", "sources"}, MANIFEST_FILENAME)
    if type(raw["schema_version"]) is not int or raw["schema_version"] != MANIFEST_VERSION:
        raise MarksError(f"{MANIFEST_FILENAME}: unsupported schema_version")
    _fields(raw["sources"], set(SOURCE_FILENAMES), f"{MANIFEST_FILENAME}.sources")
    for filename, item in raw["sources"].items():
        where = f"{MANIFEST_FILENAME}.sources.{filename}"
        _fields(item, {"sha256", "size_bytes"}, where)
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise MarksError(f"{where}: expected a lowercase SHA-256 digest")
        if type(item["size_bytes"]) is not int or not 0 <= item["size_bytes"] <= MAX_SOURCE_BYTES:
            raise MarksError(f"{where}: invalid size_bytes")
    body = {key: raw[key] for key in ("schema_version", "sources")}
    expected = hashlib.sha256(_canonical_json(body)).hexdigest()
    if raw["batch_id"] != expected:
        raise MarksError(f"{MANIFEST_FILENAME}: batch_id does not match its source manifest")
    return raw

#_signature()Builds a file-metadata signature used to detect changes.
def _signature(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

#_source_stat()Checks a file’s existence, type, size, and metadata.
def _source_stat(path: Path, *, optional: bool = False) -> tuple[int, ...] | None:
    try:
        value = path.stat()
    except FileNotFoundError as exc:
        if optional and not path.is_symlink():
            return None
        raise MarksError(f"{path.name}: cannot read {path}: {exc.strerror or exc}") from exc
    except OSError as exc:
        raise MarksError(f"{path.name}: cannot read {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(value.st_mode):
        raise MarksError(f"{path.name}: expected a regular input file")
    return _signature(value)

#_read_stable_file()Reads a file while checking that its identity and metadata remain consistent.
def _read_stable_file(path: Path, expected: tuple[int, ...], limit: int) -> bytes:
    """Read once, checking both pathname and open-file identity around the read."""
    try:
        # A source replaced by a FIFO must fail instead of blocking on open().
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or _signature(before) != expected:
                raise MarksError(f"{path.name}: input changed while reading; retry a stable export")
            data = handle.read(limit + 1)
            if _signature(os.fstat(handle.fileno())) != expected:
                raise MarksError(f"{path.name}: input changed while reading; retry a stable export")
    except OSError as exc:
        raise MarksError(f"{path.name}: cannot read {path}: {exc.strerror or exc}") from exc
    if len(data) > limit:
        raise MarksError(f"{path.name}: exceeds the {limit}-byte input limit")
    return data

# _check_snapshot()Checks whether any source file changed during capture.
def _check_snapshot(directory: Path, state: Mapping[str, tuple[int, ...] | None]) -> None:
    for filename, expected in state.items():
        try:
            actual = _source_stat(directory / filename, optional=filename == MANIFEST_FILENAME)
        except MarksError as exc:
            raise MarksError(f"{filename}: input changed while reading; retry a stable export") from exc
        if actual != expected:
            raise MarksError(f"{filename}: input changed while reading; retry a stable export")

#_capture_sources()Captures all four files and verifies an optional manifest.
def _capture_sources(directory: Path, require_manifest: bool) -> tuple[dict[str, bytes], dict]:
    """Pin a manifest, then capture every source once and verify the entire set."""
    state = {MANIFEST_FILENAME: _source_stat(directory / MANIFEST_FILENAME, optional=True)}
    manifest = None
    if state[MANIFEST_FILENAME] is None:
        if require_manifest:
            raise MarksError(f"{MANIFEST_FILENAME}: required sealed input batch manifest is missing")
    else:
        manifest = _parse_manifest(_read_stable_file(
            directory / MANIFEST_FILENAME, state[MANIFEST_FILENAME], MAX_MANIFEST_BYTES))
    # Baseline all files before reading any one of them, not just before each read.
    for filename in SOURCE_FILENAMES:
        state[filename] = _source_stat(directory / filename)
    sources = {filename: _read_stable_file(directory / filename, state[filename], MAX_SOURCE_BYTES)
               for filename in SOURCE_FILENAMES}
    _check_snapshot(directory, state)
    if manifest is not None:
        actual = _manifest_for_bytes(sources)
        for filename in SOURCE_FILENAMES:
            if actual["sources"][filename] != manifest["sources"][filename]:
                raise MarksError(f"{filename}: source bytes do not match pinned {MANIFEST_FILENAME}")
    return sources, state

#_decode_sources()Converts captured bytes into UTF-8 text; rejects invalid encoding.
def _decode_sources(sources: Mapping[str, bytes]) -> dict[str, str]:
    decoded = {}
    for filename, data in sources.items():
        try:
            decoded[filename] = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MarksError(f"{filename}: invalid UTF-8 at byte {exc.start}") from exc
    return decoded


def load_dataset(directory: Path, require_manifest: bool = False) -> Dataset:
    """Read a verified content bundle, or a best-effort legacy directory snapshot.

    A present manifest is always enforced. Strict mode requires one: all returned
    bytes must belong to that pinned, declared bundle. Legacy metadata checks
    detect ordinary concurrent writes but cannot establish a single business
    generation across separately authored files. Neither mode authenticates the
    exporter; a stable, authoritative export is the writer's responsibility.
    """
    directory = Path(directory)
    sources, state = _capture_sources(directory, require_manifest)
    dataset = parse_sources(_decode_sources(sources))
    _check_snapshot(directory, state)
    return dataset


'''
load_dataset()
    → capture file bytes
    → decode into text
    → parse_sources()
        → validate cap tables
        → validate CSV observations
        → return Dataset
'''