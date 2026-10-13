"""Validated, deterministic what-if inputs; source observations remain untouched."""

from __future__ import annotations

from .domain import MarksError
from .inputs import _decimal, _label


FIELDS = frozenset({"arr", "multiple", "net_cash"})


def normalize_overrides(value: object, companies=None) -> list[dict]:
    """Validate the structured request and return one canonical, sorted list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise MarksError("overrides: expected a list")
    normalized, seen = [], set()
    for index, item in enumerate(value, 1):
        where = f"override {index}"
        if not isinstance(item, dict) or set(item) != {"company", "field", "value"}:
            raise MarksError(f"{where}: expected exactly company, field, and value")
        company = _label(item["company"], f"{where} company")
        field = item["field"]
        if not isinstance(field, str) or field not in FIELDS:
            raise MarksError(f"{where}: field must be arr, multiple, or net_cash")
        if companies is not None and company not in companies:
            raise MarksError(f"{where}: unknown company {company!r}")
        if (company, field) in seen:
            raise MarksError(f"{where}: duplicate override for {company}.{field}")
        if not isinstance(item["value"], str):
            raise MarksError(f"{where} value: expected a decimal string")
        number = _decimal(item["value"], f"{where} value", signed=field == "net_cash")
        text = format(number, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        normalized.append({"company": company, "field": field, "value": text})
        seen.add((company, field))
    return sorted(normalized, key=lambda item: (item["company"], item["field"]))


def parse_overrides(values: list[str]) -> list[dict]:
    """Parse repeated 'Company.field=value' arguments without changing any files."""
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise MarksError("overrides: expected a list of Company.field=value strings")
    parsed = []
    for value in values:
        target, separator, number = value.partition("=")
        company, dot, field = target.rpartition(".")
        if not separator or not dot:
            raise MarksError(f"override {value!r}: expected Company.field=value")
        parsed.append({"company": company, "field": field, "value": number})
    return normalize_overrides(parsed)
