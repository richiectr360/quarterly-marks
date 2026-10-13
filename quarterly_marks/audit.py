"""Portable, verified run receipts. Hashes detect changes; they do not prove authorship.

Receipts carry source text, never executable code. Replay uses the installed engine
only after checking its version and source fingerprints. Comparison receipts embed
their two financial parents so they remain useful without the original files.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from . import __version__
from .domain import ATTRIBUTION_ORDER, DECIMAL_CONTEXT, AuditError, Dataset, MarksError, Quarter, parse_date
from .inputs import MAX_SOURCE_BYTES, SOURCE_FILENAMES, parse_sources
from .scenarios import normalize_overrides


SCHEMA_VERSION = 3
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
CODE_FILES = (
    "quarterly_marks/domain.py",
    "quarterly_marks/inputs.py",
    "quarterly_marks/selection.py",
    "quarterly_marks/scenarios.py",
    "quarterly_marks/valuation.py",
    "waterfall.py",
)
_BASE = Path(__file__).resolve().parent.parent
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_COMMON_KEYS = {"schema_version", "run_id", "created_at", "operation", "parent_run_ids", "integrity_sha256"}
_FINANCIAL_KEYS = _COMMON_KEYS | {"engine", "request", "sources", "report"}
_COMPARISON_KEYS = _COMMON_KEYS | {"left_receipt", "right_receipt", "comparison"}
_MISSING = object()
_REQUEST_KEYS = {"quarter", "as_of", "hypothetical", "overrides", "include_attribution"}


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuditError("audit content must be finite, UTF-8 encodable JSON") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _seal(receipt: dict) -> dict:
    receipt["integrity_sha256"] = _sha256(_canonical(
        {key: value for key, value in receipt.items() if key != "integrity_sha256"}))
    return receipt


def _engine_fingerprint() -> dict:
    hashes = {}
    for filename in CODE_FILES:
        try:
            hashes[filename] = _sha256((_BASE / filename).read_bytes())
        except OSError as exc:
            raise AuditError(f"cannot fingerprint calculation code {filename}: {exc.strerror or exc}") from exc
    return {"version": __version__, "code_sha256": hashes,
            "runtime": {"implementation": platform.python_implementation(),
                        "python": platform.python_version()}}


def _header(operation: str, parents: list[str]) -> dict:
    return {"schema_version": SCHEMA_VERSION, "run_id": str(uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation, "parent_run_ids": list(parents)}


def _keys(value: Any, expected: set[str], where: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise AuditError(f"{where}: invalid object fields")


def _uuid(value: Any, where: str) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise AuditError(f"{where}: expected a canonical UUID") from exc


def _hash(value: Any, where: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise AuditError(f"{where}: expected a SHA-256 digest")


def _company_rows(report: dict) -> dict[str, dict]:
    rows = report.get("companies")
    if not isinstance(rows, list) or not rows:
        raise AuditError("report.companies: expected a nonempty list")
    result = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("company"), str) or not row["company"].strip():
            raise AuditError(f"report.companies[{index}]: expected a named company object")
        if row["company"] in result:
            raise AuditError(f"report.companies: duplicate company {row['company']!r}")
        result[row["company"]] = row
    return result


def _numeric_text(value: Any, where: str, *, nullable: bool = False,
                  nonnegative: bool = False, whole: bool = False) -> Decimal | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > 128 or not _NUMBER.fullmatch(value):
        raise AuditError(f"{where}: expected a finite decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise AuditError(f"{where}: expected a finite decimal string") from exc
    if not number.is_finite() or (nonnegative and number < 0) or (whole and number != number.to_integral_value()):
        raise AuditError(f"{where}: invalid financial value")
    return number


def _messages(value: Any, where: str) -> None:
    if not isinstance(value, list) or any(
            not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key]
                                                 for key in ("code", "message")) for item in value):
        raise AuditError(f"{where}: expected messages with code and message")


def _validate_attribution(row: dict, requested: bool) -> None:
    bridge = row.get("attribution")
    if not requested:
        if bridge is not None:
            raise AuditError("unrequested attribution in report")
        return
    _keys(bridge, {"available", "reason", "order", "steps", "total_change", "interaction", "caveat"}, "attribution")
    if type(bridge["available"]) is not bool or bridge["order"] != list(ATTRIBUTION_ORDER):
        raise AuditError("attribution: invalid availability or order")
    if not isinstance(bridge["caveat"], str) or not bridge["caveat"]:
        raise AuditError("attribution: missing interpretation")
    if not bridge["available"]:
        if (not isinstance(bridge["reason"], str) or not bridge["reason"] or bridge["steps"] != []
                or bridge["total_change"] is not None or bridge["interaction"] is not None):
            raise AuditError("unavailable attribution requires a reason and no amounts")
        return
    if row["status"] != "marked" or row["prior_quarter_mark"] is None or bridge["reason"] is not None:
        raise AuditError("attribution requires current and published prior marks")
    steps = bridge["steps"]
    if not isinstance(steps, list) or len(steps) != len(ATTRIBUTION_ORDER):
        raise AuditError("attribution: invalid steps")
    with localcontext(DECIMAL_CONTEXT):
        values = []
        for index, (step, label) in enumerate(zip(steps, ATTRIBUTION_ORDER)):
            _keys(step, {"step", "fund_value", "effect", "calculation"}, "attribution step")
            if step["step"] != label:
                raise AuditError("attribution: step order differs")
            value = _numeric_text(step["fund_value"], "attribution fund value", nonnegative=True, whole=True)
            effect = _numeric_text(step["effect"], "attribution effect", nullable=index == 0, whole=True)
            if index == 0:
                if effect is not None or step["calculation"] is not None or value != Decimal(row["prior_quarter_mark"]):
                    raise AuditError("attribution: invalid published baseline")
            elif (effect != value - values[-1] or not isinstance(step["calculation"], dict)
                  or step["calculation"].get("rounded_fund_position_value") != step["fund_value"]):
                raise AuditError("attribution: effect or calculation does not reconcile")
            values.append(value)
        total = _numeric_text(bridge["total_change"], "attribution total change", whole=True)
        if total != values[-1] - values[0] or values[-1] != Decimal(row["rounded_fund_position_value"]):
            raise AuditError("attribution: total does not reconcile to current mark")
        interaction = bridge["interaction"]
        _keys(interaction, {"basis", "arr_alone_effect", "multiple_alone_effect", "joint_effect",
                            "interaction_effect", "multiple_only_calculation"}, "attribution interaction")
        isolated = interaction["multiple_only_calculation"]
        if not isinstance(interaction["basis"], str) or not isinstance(isolated, dict):
            raise AuditError("attribution: invalid interaction evidence")
        multiple_only = _numeric_text(isolated.get("rounded_fund_position_value"), "multiple-only mark", nonnegative=True, whole=True)
        expected = {"arr_alone_effect": values[2] - values[1], "multiple_alone_effect": multiple_only - values[1],
                    "joint_effect": values[3] - values[1], "interaction_effect": values[3] - values[2] - multiple_only + values[1]}
        for key, value in expected.items():
            if _numeric_text(interaction[key], key, whole=True) != value:
                raise AuditError("attribution: interaction does not reconcile")


def _validate_report(report: dict, quarter: Quarter, as_of: str | None) -> None:
    if report.get("quarter_end") != quarter.end.isoformat():
        raise AuditError("report.quarter_end does not match the requested quarter")
    if report.get("cutoff_mode") != ("as_of" if as_of is not None else "latest_supplied"):
        raise AuditError("report.cutoff_mode does not match the requested as_of")
    if not isinstance(report.get("conventions"), dict):
        raise AuditError("report.conventions: expected an object")
    rows = _company_rows(report)
    marked = sum(row.get("status") == "marked" for row in rows.values())
    if (type(report.get("marked_company_count")) is not int or report["marked_company_count"] != marked
            or report.get("status") != ("complete" if marked == len(rows) else "incomplete")):
        raise AuditError("report completeness does not match company rows")
    for company, row in rows.items():
        where = f"report company {company!r}"
        if row.get("status") not in ("marked", "no_mark"):
            raise AuditError(f"{where}: invalid status")
        incomplete = row["status"] == "no_mark"
        _messages(row.get("errors"), where + ".errors")
        missing = [field for field in ("arr", "multiple") if row.get(field) is None]
        if (row.get("missing_inputs") != missing or bool(missing) != incomplete
                or bool(row["errors"]) != incomplete):
            raise AuditError(f"{where}: inconsistent missing inputs or errors")
        unavailable = {"enterprise_value", "equity_value", "ownership_pct", "fund_position_value",
                       "rounded_fund_position_value", "arr_growth_pct", "mark_change_pct"}
        if incomplete and (any(row.get(field) is not None for field in unavailable)
                           or row.get("class_allocations") != {} or row.get("fund_contributions") != {}):
            raise AuditError(f"{where}: no-mark row contains calculated results")
        for field in ("arr", "multiple", "enterprise_value", "net_cash", "equity_value", "ownership_pct", "fund_position_value",
                      "rounded_fund_position_value", "prior_quarter_mark", "arr_growth_pct", "mark_change_pct"):
            if field not in row:
                raise AuditError(f"{where}: missing financial field {field}")
            value = _numeric_text(row[field], f"{where}.{field}",
                                  nullable=field in {"prior_quarter_mark", "arr_growth_pct", "mark_change_pct"}
                                  or (incomplete and field != "net_cash"),
                                  nonnegative=field not in {"arr_growth_pct", "mark_change_pct", "net_cash"},
                                  whole=field in {"rounded_fund_position_value", "prior_quarter_mark"})
            if field == "ownership_pct" and value is not None and value > 100:
                raise AuditError(f"{where}.ownership_pct: cannot exceed 100")
        for value_field, reason_field in (("arr_growth_pct", "arr_growth_reason"), ("mark_change_pct", "mark_change_reason")):
            if reason_field not in row or (row[reason_field] is not None and not isinstance(row[reason_field], str)):
                raise AuditError(f"{where}.{reason_field}: expected text or null")
            if row[value_field] is None and not row[reason_field]:
                raise AuditError(f"{where}.{reason_field}: missing values require a reason")
        for field in ("selected_inputs", "input_history", "cap_table", "class_allocations", "fund_contributions"):
            if not isinstance(row.get(field), dict):
                raise AuditError(f"{where}.{field}: expected an object")
        for field in ("class_allocations", "fund_contributions"):
            for name, amount in row[field].items():
                _numeric_text(amount, f"{where}.{field}.{name}", nonnegative=True)
        _messages(row.get("warnings"), where + ".warnings")
        _validate_attribution(row, report["include_attribution"])


def _validate_financial(receipt: dict, require_compatible: bool) -> None:
    operation, parents = receipt["operation"], receipt["parent_run_ids"]
    if (operation == "calculate" and parents) or (operation == "replay" and len(parents) != 1):
        raise AuditError(f"{operation} receipt has invalid parent run IDs")
    engine = receipt["engine"]
    _keys(engine, {"version", "code_sha256", "runtime"} if isinstance(engine, dict) and "runtime" in engine
          else {"version", "code_sha256"}, "engine")
    if "runtime" in engine:
        _keys(engine["runtime"], {"implementation", "python"}, "engine.runtime")
        if any(not isinstance(value, str) or not value for value in engine["runtime"].values()):
            raise AuditError("engine.runtime: expected nonempty implementation and Python version")
    if not isinstance(engine["version"], str) or not engine["version"]:
        raise AuditError("engine.version: expected a nonempty version")
    _keys(engine["code_sha256"], set(CODE_FILES), "engine.code_sha256")
    for filename, digest in engine["code_sha256"].items():
        _hash(digest, f"engine.code_sha256.{filename}")
    _keys(receipt["request"], _REQUEST_KEYS, "request")
    request, report = receipt["request"], receipt["report"]
    if not isinstance(report, dict):
        raise AuditError("report: expected an object")
    if not isinstance(request["quarter"], str):
        raise AuditError("request.quarter: expected a quarter string")
    try:
        quarter = Quarter.parse(request["quarter"])
        if request["as_of"] is not None:
            cutoff = parse_date(request["as_of"], "request.as_of")
            if cutoff < quarter.end:
                raise AuditError("request.as_of precedes quarter-end")
    except MarksError as exc:
        raise AuditError(f"invalid audit request: {exc}") from exc
    if not isinstance(request["overrides"], list):
        raise AuditError("request.overrides: expected a list")
    try:
        normalized = normalize_overrides(request["overrides"], companies=_company_rows(report))
    except MarksError as exc:
        raise AuditError(f"invalid audit overrides: {exc}") from exc
    if normalized != request["overrides"]:
        raise AuditError("request.overrides must be normalized")
    if type(request["hypothetical"]) is not bool or request["hypothetical"] != bool(normalized):
        raise AuditError("request.hypothetical must match its overrides")
    if any(key not in report or report[key] != request[key] for key in request):
        raise AuditError("report does not match the captured request")
    if type(report["hypothetical"]) is not bool:
        raise AuditError("report.hypothetical: expected a boolean")
    if type(request["include_attribution"]) is not bool or type(report["include_attribution"]) is not bool:
        raise AuditError("include_attribution: expected a boolean")
    _validate_report(report, quarter, request["as_of"])
    _keys(receipt["sources"], set(SOURCE_FILENAMES), "sources")
    for filename, source in receipt["sources"].items():
        _keys(source, {"text", "sha256"}, f"sources.{filename}")
        if not isinstance(source["text"], str):
            raise AuditError(f"sources.{filename}.text: expected UTF-8 text")
        try:
            raw = source["text"].encode("utf-8")
        except UnicodeError as exc:
            raise AuditError(f"sources.{filename}: invalid UTF-8 text") from exc
        if len(raw) > MAX_SOURCE_BYTES:
            raise AuditError(f"sources.{filename}: source exceeds {MAX_SOURCE_BYTES} bytes")
        _hash(source["sha256"], f"sources.{filename}.sha256")
        if _sha256(raw) != source["sha256"]:
            raise AuditError(f"source hash mismatch: {filename}")
    if require_compatible:
        current = _engine_fingerprint()
        if engine["version"] != current["version"]:
            raise AuditError(f"incompatible engine version {engine['version']!r}; current version is {current['version']!r}")
        changed = [name for name in CODE_FILES if engine["code_sha256"][name] != current["code_sha256"][name]]
        if changed:
            raise AuditError("calculation code has changed; replay requires the recorded engine: " + ", ".join(changed))
        if "runtime" in engine:
            recorded, running = engine["runtime"], current["runtime"]
            if (recorded["implementation"] != running["implementation"]
                    or recorded["python"].split(".")[0] != running["python"].split(".")[0]):
                raise AuditError("Python runtime is incompatible; replay requires the recorded implementation and major version")
            # Minor/patch differences are permitted: replay still compares every
            # report field, and the CLI discloses the runtime change after success.


def _validate_receipt(receipt: Any, *, require_compatible: bool, nested: bool = False) -> None:
    if not isinstance(receipt, dict):
        raise AuditError("audit receipt must be a JSON object")
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != SCHEMA_VERSION:
        raise AuditError(f"unsupported audit schema version {receipt.get('schema_version')!r}")
    operation = receipt.get("operation")
    if operation not in ("calculate", "replay", "compare"):
        raise AuditError("unsupported audit operation")
    if nested and operation == "compare":
        raise AuditError("comparison parents must be financial receipts, not nested comparisons")
    _keys(receipt, _COMPARISON_KEYS if operation == "compare" else _FINANCIAL_KEYS, "receipt")
    _uuid(receipt["run_id"], "run_id")
    try:
        timestamp = datetime.fromisoformat(receipt["created_at"])
        if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise AuditError("created_at: expected a UTC ISO timestamp") from exc
    parents = receipt["parent_run_ids"]
    if not isinstance(parents, list) or len(parents) > 2:
        raise AuditError("parent_run_ids: expected at most two UUIDs")
    for parent in parents:
        _uuid(parent, "parent_run_ids")
    if receipt["run_id"] in parents:
        raise AuditError("a receipt cannot be its own parent")
    _hash(receipt["integrity_sha256"], "integrity_sha256")
    expected = _sha256(_canonical({key: value for key, value in receipt.items() if key != "integrity_sha256"}))
    if receipt["integrity_sha256"] != expected:
        raise AuditError("audit integrity hash mismatch; the receipt has changed")
    if operation == "compare":
        left, right = receipt["left_receipt"], receipt["right_receipt"]
        _validate_receipt(left, require_compatible=False, nested=True)
        _validate_receipt(right, require_compatible=False, nested=True)
        if parents != [left["run_id"], right["run_id"]]:
            raise AuditError("comparison parent IDs do not match embedded receipts")
        expected_comparison = compare_receipts(left, right)
        mismatch = _first_difference(expected_comparison, receipt["comparison"], "comparison")
        if mismatch:
            raise AuditError(f"comparison result does not match embedded receipts at {mismatch}")
    else:
        try:
            _validate_financial(receipt, require_compatible)
        except DecimalException as exc:
            raise AuditError("invalid arithmetic in audit financial values") from exc


def _finish_receipt(receipt: dict) -> dict:
    """Detach caller objects, seal once, and validate the resulting receipt."""
    receipt = json.loads(_canonical(receipt))
    _seal(receipt)
    _validate_receipt(receipt, require_compatible=False)
    return receipt


def create_receipt(dataset: Dataset, report: dict, *, operation: str = "calculate",
                   parent_run_ids: list[str] | None = None) -> dict:
    """Capture one complete calculation. Mutable caller objects are copied into JSON."""
    if operation not in ("calculate", "replay"):
        raise AuditError("use create_comparison_receipt for comparisons")
    if not isinstance(report, dict) or "quarter" not in report or "as_of" not in report:
        raise AuditError("report must identify quarter and as_of")
    sources = {}
    for filename, text in dataset.sources.items():
        if not isinstance(text, str):
            raise AuditError(f"source {filename}: expected UTF-8 text")
        try:
            sources[filename] = {"text": text, "sha256": _sha256(text.encode("utf-8"))}
        except UnicodeError as exc:
            raise AuditError(f"source {filename}: invalid UTF-8 text") from exc
    receipt = _header(operation, parent_run_ids if parent_run_ids is not None else [])
    receipt.update(engine=_engine_fingerprint(),
                   request={key: report.get(key) for key in _REQUEST_KEYS},
                   sources=sources, report=report)
    return _finish_receipt(receipt)


def create_comparison_receipt(first: dict, second: dict, comparison: dict) -> dict:
    """Capture a comparison and its financial parents, without revaluing those parents."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise AuditError("comparison parents must be receipt objects")
    receipt = _header("compare", [first.get("run_id"), second.get("run_id")])
    receipt.update(left_receipt=first, right_receipt=second, comparison=comparison)
    return _finish_receipt(receipt)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError(f"audit JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AuditError(f"audit JSON contains non-finite number {value}")


def load_receipt(path: Path, *, require_compatible: bool = True) -> dict:
    """Read bounded JSON and validate schema, source hashes, integrity and compatibility."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
    except OSError as exc:
        raise AuditError(f"cannot read audit {path}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_RECEIPT_BYTES:
        raise AuditError(f"audit exceeds {MAX_RECEIPT_BYTES} bytes")
    try:
        receipt = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AuditError(f"invalid audit JSON in {path}: {type(exc).__name__}") from exc
    _validate_receipt(receipt, require_compatible=require_compatible)
    return receipt


def write_receipt(receipt: dict, directory: Path) -> Path:
    """Publish a complete receipt atomically without overwriting earlier evidence."""
    _validate_receipt(receipt, require_compatible=False)
    try:
        encoded = (json.dumps(receipt, sort_keys=True, indent=2, ensure_ascii=False,
                              allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuditError("cannot serialize audit receipt") from exc
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise AuditError(f"audit exceeds {MAX_RECEIPT_BYTES} bytes")
    try:
        directory = Path(directory).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuditError(f"cannot resolve audit directory: {exc}") from exc
    destination = directory / f"{receipt['operation']}-{receipt['run_id']}.json"
    temporary = None
    published = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".marks-", suffix=".tmp",
                                         dir=directory, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Linking a fully-written inode is atomic and fails if destination exists.
        os.link(temporary, destination)
        published = True
        temporary.unlink()
        temporary = None
        verified = load_receipt(destination, require_compatible=False)
        if _canonical(verified) != _canonical(receipt):
            raise AuditError("audit read-back differs from the intended receipt")
        return destination
    except (OSError, AuditError) as exc:
        if published:
            raise AuditError(
                f"receipt published at {destination}, but completion could not be confirmed; "
                f"retained for verification: {exc}"
            ) from exc
        if isinstance(exc, AuditError):
            raise
        raise AuditError(f"cannot publish audit {destination}: {exc.strerror or exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _first_difference(expected: Any, actual: Any, path: str = "report") -> str | None:
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return f"{path}.{key}"
            difference = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}.length"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
    elif expected != actual:
        return path
    return None


def replay_receipt(path: Path) -> tuple[dict, dict]:
    """Recompute from captured inputs, never from current CSVs or embedded code."""
    saved = load_receipt(path)
    if saved["operation"] == "compare":
        return saved, compare_receipts(saved["left_receipt"], saved["right_receipt"])
    try:
        dataset = parse_sources({name: source["text"] for name, source in saved["sources"].items()})
        request = saved["request"]
        quarter = Quarter.parse(request["quarter"])
        as_of = parse_date(request["as_of"]) if request["as_of"] is not None else None
        from .valuation import build_report
        report = build_report(dataset, quarter, as_of, overrides=request["overrides"],
                              include_attribution=request["include_attribution"])
    except MarksError as exc:
        raise AuditError(f"captured inputs could not be replayed: {exc}") from exc
    mismatch = _first_difference(saved["report"], report)
    if mismatch:
        raise AuditError(f"replay result mismatch at {mismatch}")
    return saved, report


def _same_decimal(left: str, right: str) -> bool:
    if len(left) > 128 or len(right) > 128 or not _NUMBER.fullmatch(left) or not _NUMBER.fullmatch(right):
        return False
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return False


def _differences(left: Any, right: Any, path: str = "", *, numeric: bool = True) -> list[dict]:
    if left is not _MISSING and right is not _MISSING and type(left) is type(right):
        if left == right:
            return []
        # Raw source cells and human-authored notes keep their exact text meaning.
        exact_text = ".raw." in path or path.endswith((".note", ".message", ".filename", ".entered_by"))
        if numeric and not exact_text and isinstance(left, str) and _same_decimal(left, right):
            return []
        if isinstance(left, dict):
            changes = []
            for key in sorted(set(left) | set(right)):
                changes.extend(_differences(left.get(key, _MISSING), right.get(key, _MISSING),
                                            f"{path}.{key}" if path else key, numeric=numeric))
            return changes
        if isinstance(left, list) and len(left) == len(right):
            changes = []
            for index, (a, b) in enumerate(zip(left, right)):
                changes.extend(_differences(a, b, f"{path}[{index}]", numeric=numeric))
            return changes
    difference = {"field": path, "before": None if left is _MISSING else left,
                  "after": None if right is _MISSING else right}
    if left is _MISSING:
        difference["before_present"] = False
    if right is _MISSING:
        difference["after_present"] = False
    return [difference]


def compare_receipts(first: dict, second: dict) -> dict:
    """Describe changed inputs and results; differences are not causal attribution.

    Call load_receipt(..., require_compatible=False) first for files. Comparisons
    intentionally permit historical engines and do not revalue either parent.
    """
    if any(receipt.get("operation") not in ("calculate", "replay") for receipt in (first, second)):
        raise AuditError("comparisons require two financial receipts")
    left, right = _company_rows(first["report"]), _company_rows(second["report"])
    added, removed = sorted(right.keys() - left.keys()), sorted(left.keys() - right.keys())
    changes = []
    structural = {"company", "selected_inputs", "warnings", "input_history", "cap_table"}
    for company in sorted(left.keys() & right.keys()):
        a, b = left[company], right[company]
        change = {
            "company": company,
            "financial_changes": _differences({k: v for k, v in a.items() if k not in structural},
                                                {k: v for k, v in b.items() if k not in structural}),
            "selected_input_changes": _differences(a.get("selected_inputs"), b.get("selected_inputs"), "selected_inputs"),
            "cap_table_changes": _differences(a.get("cap_table"), b.get("cap_table"), "cap_table"),
            "warning_changes": _differences(a.get("warnings"), b.get("warnings"), "warnings", numeric=False),
        }
        if any(value for key, value in change.items() if key != "company"):
            changes.append(change)
    report_changes = _differences({k: v for k, v in first["report"].items() if k != "companies"},
                                 {k: v for k, v in second["report"].items() if k != "companies"})
    source_changes = sorted(name for name in SOURCE_FILENAMES
                            if first["sources"][name]["sha256"] != second["sources"][name]["sha256"])
    engine_changes = _differences(first["engine"], second["engine"], "engine", numeric=False)
    request_changes = _differences(first["request"], second["request"], numeric=False)
    return {
        "first_run_id": first["run_id"], "second_run_id": second["run_id"],
        "first_request": dict(first["request"]), "second_request": dict(second["request"]),
        "has_changes": bool(added or removed or changes or report_changes or source_changes or engine_changes or request_changes),
        "added_companies": added, "removed_companies": removed,
        "changed_company_count": len(changes), "changes": changes,
        "report_changes": report_changes, "changed_source_files": source_changes,
        "engine_changes": engine_changes,
        "request_changes": request_changes,
        "interpretation": "Observed input and result differences; no causal attribution or revaluation is implied.",
    }
