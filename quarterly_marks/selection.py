"""Date-based selection that never substitutes file order for version history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .domain import MarksError, Observation, Quarter

# Which record should we use for this company and quarter, given what we knew by the cutoff?

#ObservationIndex.__init__()Groups observations by company and quarter so they can be found quickly. It does not choose a revision.
class ObservationIndex:
    """Build once per source/run; look up only one company's quarter history.

    Indexing does not resolve revisions or discard future observations. The
    public selector remains responsible for cutoff, conflicts and provenance.
    """

    def __init__(self, observations: Iterable[Observation]):
        grouped: dict[tuple[str, Quarter], list[Observation]] = {}
        for row in observations:
            grouped.setdefault((row.company, row.quarter), []).append(row)
        self._by_key = {key: tuple(rows) for key, rows in grouped.items()}

    #ObservationIndex.get()Returns the observations for a particular company and quarter.
    def get(self, company: str, quarter: Quarter | None) -> tuple[Observation, ...]:
        return self._by_key.get((company, quarter), ()) if quarter is not None else ()


@dataclass(frozen=True)
class Selection:
    selected: Observation | None
    eligible: tuple[Observation, ...] = ()
    excluded: tuple[Observation, ...] = ()
    duplicates: tuple[Observation, ...] = ()

    #Selection.revisionsReturns earlier eligible versions, excluding identical duplicate deliveries.
    @property
    def revisions(self) -> tuple[Observation, ...]:
        """Earlier eligible versions, with repeated identical deliveries collapsed."""
        if self.selected is None:
            return ()
        duplicates = frozenset(self.duplicates)
        return tuple(row for row in self.eligible
                     if row.known_on < self.selected.known_on and row not in duplicates)
    
    # Selection.history()Creates a dictionary of eligible records, excluded later records, and ignored duplicates for auditing.
    def history(self) -> dict:
        return {
            "eligible": [row.to_dict() for row in self.eligible],
            "excluded_after_cutoff": [row.to_dict() for row in self.excluded],
            "identical_duplicates_ignored": [row.to_dict() for row in self.duplicates],
        }

# _order()Provides a consistent sorting order using date and source details.
def _order(row: Observation) -> tuple:
    return (row.known_on, row.source.filename, row.source.row_number,
            tuple(sorted(row.source.cells)))

# select_latest()Applies the cutoff, checks duplicates/conflicts, and selects the latest eligible observation.
def select_latest(
    observations: Iterable[Observation], company: str, quarter: Quarter | None,
    as_of: date | None, *, label: str,
) -> Selection:
    """Select the latest eligible version; reject ambiguity at *any* eligible date.

    A same-date value with different author/note/raw metadata is a conflict too.
    Identical source rows are harmless redeliveries: retain their provenance and
    select the lowest source row number deterministically.
    """
    rows = sorted((row for row in observations
                   if row.company == company and row.quarter == quarter), key=_order)
    eligible = tuple(row for row in rows if as_of is None or row.known_on <= as_of)
    excluded = tuple(row for row in rows if as_of is not None and row.known_on > as_of)
    versions: dict[date, Observation] = {}
    duplicates = []
    for row in eligible:
        previous = versions.get(row.known_on)
        if previous is None:
            versions[row.known_on] = row
        elif (row.value != previous.value
              or dict(row.source.cells) != dict(previous.source.cells)):
            raise MarksError(
                f"{company} {quarter} {label}: conflicting versions on {row.known_on}; "
                f"{previous.source.filename} rows {previous.source.row_number} and "
                f"{row.source.row_number} disagree in value or source metadata"
            )
        else:
            duplicates.append(row)
    selected = versions[max(versions)] if versions else None
    return Selection(selected, eligible, excluded, tuple(duplicates))

'''
Find records matching company and quarter
        ↓
Keep records dated on or before the cutoff
        ↓
Check records sharing the same date
        ↓
Select the latest eligible date
        ↓
Return the selected record and its supporting history
'''