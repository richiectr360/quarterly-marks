"""Typed source records and explicit financial/date conventions."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, ROUND_HALF_UP
from typing import Mapping


class MarksError(Exception):
    """An actionable input or calculation failure, safe to display at the CLI."""


class AuditError(MarksError):
    """An audit could not be saved, validated, or reproduced."""


# A fresh local Context makes results independent of callers' decimal settings.
DECIMAL_CONTEXT = Context(prec=60, rounding=ROUND_HALF_UP)
DOLLAR = Decimal("1")
RECONCILIATION_TOLERANCE = Decimal("0.00000001")
ARR_REVIEW_THRESHOLD = Decimal("0.50")
ATTRIBUTION_ORDER = ("Published prior", "Recalculated prior", "ARR", "Multiple", "Capitalization", "Net cash")


def parse_date(text: str, label: str = "date") -> date:
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise MarksError(f"{label}: expected YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise MarksError(f"{label}: invalid calendar date {text!r}") from exc


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    number: int

    def __post_init__(self) -> None:
        if type(self.year) is not int or type(self.number) is not int or not 1 <= self.year <= 9999 or not 1 <= self.number <= 4:
            raise MarksError("quarter: year must be 0001–9999 and quarter must be 1–4")

    @classmethod
    def parse(cls, text: str) -> Quarter:
        match = re.fullmatch(r"([0-9]{4})Q([1-4])", text) if isinstance(text, str) else None
        if not match:
            raise MarksError("quarter: expected YYYYQ1, YYYYQ2, YYYYQ3, or YYYYQ4")
        return cls(int(match[1]), int(match[2]))

    @property
    def end(self) -> date:
        month = self.number * 3
        return date(self.year, month, calendar.monthrange(self.year, month)[1])

    @property
    def previous(self) -> Quarter | None:
        if self.year == 1 and self.number == 1:
            return None
        return Quarter(self.year, self.number - 1) if self.number > 1 else Quarter(self.year - 1, 4)

    def __str__(self) -> str:
        return f"{self.year:04d}Q{self.number}"


@dataclass(frozen=True)
class SourceRow:
    filename: str
    row_number: int
    cells: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict:
        return {"filename": self.filename, "row_number": self.row_number, "raw": dict(self.cells)}


@dataclass(frozen=True)
class Observation:
    company: str
    quarter: Quarter
    value: Decimal
    known_on: date
    source: SourceRow

    def to_dict(self) -> dict:
        return {"company": self.company, "quarter": str(self.quarter),
                "value": str(self.value), "known_on": self.known_on.isoformat(),
                "source": self.source.to_dict()}


@dataclass(frozen=True)
class Series:
    name: str
    shares: int
    issue_price: Decimal
    issue_date: date
    seniority: int
    fund_shares: int

    def to_dict(self) -> dict:
        return {"name": self.name, "shares": self.shares, "issue_price": str(self.issue_price),
                "issue_date": self.issue_date.isoformat(), "seniority": self.seniority,
                "fund_shares": self.fund_shares}


@dataclass(frozen=True)
class CapTable:
    common_shares: int
    series: tuple[Series, ...]


@dataclass(frozen=True)
class Dataset:
    kpis: tuple[Observation, ...]
    multiples: tuple[Observation, ...]
    reported: tuple[Observation, ...]
    cap_tables: Mapping[str, CapTable]
    # Exact decoded UTF-8 file contents, including any BOM/newlines, for offline replay.
    sources: Mapping[str, str]

'''
In **[`domain.py`](/Users/amornchotsingh/Desktop/mucker/scale/candidate/quarterly_marks/domain.py)**, these are the functions and methods:

| Function / method | What it does |
|---|---|
| `parse_date(text, label)` | Converts text such as `"2026-08-14"` into a date. Rejects invalid formats or impossible dates. |
| `Quarter.__post_init__()` | Checks that the year is valid and the quarter number is between 1 and 4. Runs automatically when a `Quarter` is created. |
| `Quarter.parse(text)` | Converts `"2026Q3"` into a `Quarter` object containing year `2026` and quarter `3`. |
| `Quarter.end` | Returns the quarter’s final date. For Q3 2026: September 30. |
| `Quarter.previous` | Returns the previous quarter. For Q3 2026: Q2 2026. Returns `None` at the earliest supported quarter. |
| `Quarter.__str__()` | Converts a quarter object back into readable text: `"2026Q3"`. |
| `SourceRow.to_dict()` | Converts the source filename, row number, and original fields into a dictionary for audit evidence. |
| `Observation.to_dict()` | Converts a financial observation into a dictionary, preserving its company, quarter, value, knowledge date, and source. |
| `Series.to_dict()` | Converts a preferred share class into a dictionary containing its shares, issue price/date, seniority, and fund holdings. |

**The other definitions are containers or error types:**

- **`CapTable`** holds common shares and preferred classes.
- **`Dataset`** holds all parsed inputs and original source text.
- **`MarksError`** represents an input or calculation problem.
- **`AuditError`** represents an audit-saving, validation, or replay problem.

**This file defines the data’s structure; it does not calculate company valuations.**
'''