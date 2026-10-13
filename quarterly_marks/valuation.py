"""Deterministic valuation policy, isolated from clocks, file I/O and rendering."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, DecimalException, localcontext

import waterfall

from .domain import (
    ARR_REVIEW_THRESHOLD, ATTRIBUTION_ORDER, DECIMAL_CONTEXT, DOLLAR, RECONCILIATION_TOLERANCE,
    Dataset, MarksError, Quarter,
)
from .selection import ObservationIndex, Selection, select_latest
from .scenarios import normalize_overrides

'''
build_report()
    ↓
_company_report() for each company
    ├─ Select current and prior inputs
    ├─ Apply hypothetical overrides, if requested
    ├─ _calculate()
    ├─ _prior_recalculation()
    └─ Add comparisons and review notes
    ↓
_attribution(), if requested
    ↓
Return the complete report
'''


CONVENTIONS = {
    "currency": "USD assumed; all source monetary amounts are whole-dollar units, not thousands",
    "equity_value": "enterprise value + net_cash; net_cash defaults to zero and may only change in a hypothetical scenario",
    "knowledge_cutoff": "inclusive date; omitted cutoff means latest supplied records, never wall-clock time",
    "quarter_end": "calendar quarter-end; preferred issue_date must be on or before it",
    "ownership": "fund preferred shares / (common including option pool + active preferred shares)",
    "cap_history": "common shares and each class's fund holdings are assumed static; no fund common holdings supplied",
    "arr_growth": "current ARR / previous quarter ARR - 1; both selected using the same knowledge cutoff",
    "prior_mark": "latest eligible published prior-quarter mark, never a recomputed valuation",
    "rounding": "Decimal precision 60; ROUND_HALF_UP to whole dollars only after summing fund contributions",
    "mark_change": "rounded current fund mark / published prior-quarter dollars - 1",
    "missing_baseline": "missing or zero denominator produces null percentage with a reason",
    "review_policy": "calculate supplied values; flag ARR changes of at least 50%, declines and revisions; never infer corrections",
    "preference_effect": "flag fund waterfall value differing from pro-rata equity value by at least one dollar",
    "historical_check": "ordinary marks fail on a mismatch at the publication-date cutoff; later recalculations and explicitly overridden company marks disclose differences",
    "prior_recalculation": "recalculate prior quarter with the same cutoff and its quarter-end cap, without current overrides; retain published comparison baseline",
    "scenarios": "explicit overrides are hypothetical; original source observations and published marks are retained",
    "incomplete": "missing current ARR/multiple yields no mark; malformed or conflicting inputs and calculation failures remain fatal",
}


def _optional(selection: Selection) -> dict | None:
    return selection.selected.to_dict() if selection.selected is not None else None


def _pct(current: Decimal, previous: Decimal) -> Decimal:
    return (current / previous - 1) * 100


def _excluded_notes(streams: list[tuple[str, Selection]], as_of: date | None) -> list[dict]:
    return [{"code": "INPUT_AFTER_CUTOFF", "message":
             f"{description}: excluded {len(selection.excluded)} later record(s) after as-of {as_of}: " +
             ", ".join(f"{row.value:,f} recorded {row.known_on}" for row in selection.excluded) + "."}
            for description, selection in streams if selection.excluded]

# ARR × multiple
# = $10M × 5.5
# = $55M enterprise value

# Enterprise value + net cash
# = $55M + $0
# = $55M equity value

#_calculate() calculates one company’s value and our fund’s position for a particular quarter.

'''
- Share classes active at quarter-end - It includes preferred classes whose: 

issue_date <= quarter.end

Later classes are excluded and recorded separately.
For example, Orbitra’s September Series C is excluded from Q2 but included in Q3.

- Total shares and fund ownership percentage

Total shares = common shares + active preferred shares  
Fund ownership % = fund shares ÷ total shares × 100

For Kestrel:
Total shares = 10M common + 5M A + 5M B = 20M  
Fund ownership = 2.5M ÷ 20M × 100 = 12.5%

- Allocation to each share class
allocations = waterfall.allocate(equity, common_shares, active_series)

For Kestrel’s $55M equity:
_calculate() checks that the returned classes are correct, allocations are finite and nonnegative, and their sum matches equity within the defined tolerance.

6. Our fund’s contribution from each class

Fund contribution = class allocation × fund shares in class ÷ total class shares
For Kestrel’s Series B: 
$45,833,333.333… × 2.5M ÷ 5M = $22,916,666.666…

What it returns
A dictionary containing:
- ARR, multiple, and net cash used.
- Enterprise and equity values.
- Fund ownership percentage.
- Active and excluded share classes.
- Each class’s allocation.
- Each class’s contribution to the fund.
- Unrounded and rounded fund position values.
It does not calculate ARR growth, mark-change percentages, or select historical observations. Other functions handle those using _calculate()’s results.
'''
def _calculate(dataset: Dataset, company: str, quarter: Quarter,
               arr: Decimal, multiple: Decimal, net_cash: Decimal = Decimal(0)) -> dict:
    """Shared allocation path for the current mark and optional prior diagnostic."""
    enterprise = arr * multiple
    equity = enterprise + net_cash
    if equity < 0:
        raise MarksError(f"{company} {quarter}: scenario equity value cannot be negative")
    cap = dataset.cap_tables[company]
    active = sorted((s for s in cap.series if s.issue_date <= quarter.end), key=lambda s: s.name)
    excluded = sorted((s for s in cap.series if s.issue_date > quarter.end), key=lambda s: s.name)
    total_shares = cap.common_shares + sum(s.shares for s in active)
    if total_shares <= 0:
        raise MarksError(f"{company} {quarter}: no outstanding shares at quarter-end")
    fund_shares = sum(s.fund_shares for s in active)
    ownership = Decimal(fund_shares) / total_shares * 100
    allocations = waterfall.allocate(equity, cap.common_shares, [s.to_dict() for s in active])
    expected_classes = {"Common", *(s.name for s in active)}
    if set(allocations) != expected_classes:
        raise MarksError(f"{company} {quarter}: waterfall returned incorrect share classes")
    if any(not isinstance(value, Decimal) or not value.is_finite() or value < 0
           for value in allocations.values()):
        raise MarksError(f"{company} {quarter}: waterfall returned a non-finite or negative allocation")
    if abs(sum(allocations.values(), Decimal(0)) - equity) > RECONCILIATION_TOLERANCE:
        raise MarksError(f"{company} {quarter}: waterfall allocations do not reconcile to equity value")
    contributions = {"Common": Decimal(0)}
    for series in active:
        contribution = allocations[series.name] * series.fund_shares / series.shares
        if (not contribution.is_finite() or contribution < 0
                or contribution - allocations[series.name] > RECONCILIATION_TOLERANCE):
            raise MarksError(f"{company} {quarter}: invalid fund contribution for {series.name}")
        contributions[series.name] = contribution
    fund_value = sum(contributions.values(), Decimal(0))
    if fund_value - equity > RECONCILIATION_TOLERANCE:
        raise MarksError(f"{company} {quarter}: fund value exceeds company equity value")
    rounded_fund = fund_value.quantize(DOLLAR)
    return {
        "arr": str(arr), "multiple": str(multiple), "net_cash": str(net_cash),
        "enterprise_value": str(enterprise), "equity_value": str(equity),
        "ownership_pct": str(ownership), "fund_position_value": str(fund_value),
        "rounded_fund_position_value": str(rounded_fund),
        "cap_table": {"common_shares": cap.common_shares,
                      "active_series": [s.to_dict() for s in active],
                      "excluded_series": [s.to_dict() for s in excluded]},
        "class_allocations": {name: str(allocations[name]) for name in sorted(allocations)},
        "fund_contributions": {name: str(contributions[name]) for name in sorted(contributions)},
    }

# _prior_recalculation() asks: “What would the previous quarter’s mark be using the information eligible at this run’s cutoff?”
# It compares that recalculated value with the published amount without replacing the published amount.

# Orbitra’s published Q2 mark:     $26,666,667
# Q2 recalculated with revised 8.5× multiple: $28,333,333
# Difference:                     +$1,666,666

# 1. Gather the previous quarter’s selected inputs
# selections = {
#     "arr": chosen["prior_arr"],
#     "multiple": prior_multiple,
#     "published_mark": chosen["prior_mark"]
# }
# These observations have already been selected using the report’s knowledge cutoff.
# For Orbitra under latest supplied information:
# Input	Selected value
# Q2 ARR	$20M
# Q2 multiple	8.5×
# Published Q2 fund mark	$26,666,667

#This function does not select the revisions itself.

# Recalculate Q2
#     → use Q2 ARR
#     → use the eligible revision of Q2's multiple
#     → use share classes issued by June 30
#     → exclude September's Series C
def _prior_recalculation(dataset: Dataset, company: str, previous: Quarter | None,
                         chosen: dict[str, Selection], prior_multiple: Selection) -> dict:
    selections = {"arr": chosen["prior_arr"], "multiple": prior_multiple,
                  "published_mark": chosen["prior_mark"]}
    published = chosen["prior_mark"].selected
    diagnostic = {
        "quarter": str(previous) if previous is not None else None,
        "available": False, "reason": None,
        "selected_inputs": {key: _optional(value) for key, value in selections.items()},
        "input_history": {key: value.history() for key, value in selections.items()},
        "calculation": None, "published_mark": str(published.value) if published else None,
        "delta": None,
    }
    missing = [key for key in ("arr", "multiple") if selections[key].selected is None]
    if previous is None:
        diagnostic["reason"] = "No earlier calendar quarter"
    elif missing:
        diagnostic["reason"] = f"No eligible {', '.join(missing)} for {previous}"
    else:
        cap = dataset.cap_tables[company]
        shares = cap.common_shares + sum(s.shares for s in cap.series if s.issue_date <= previous.end)
        if shares <= 0:
            diagnostic["reason"] = f"No outstanding shares at {previous} quarter-end"
        else:
            calculation = _calculate(dataset, company, previous,
                                     selections["arr"].selected.value, selections["multiple"].selected.value)
            diagnostic["available"] = True
            diagnostic["calculation"] = calculation
            if published is not None:
                diagnostic["delta"] = str(Decimal(calculation["rounded_fund_position_value"]) - published.value)
    return diagnostic #delta = recalculated_rounded_mark - published_mark

#_attribution() explains how we move from the published previous-quarter mark to the current mark, changing one input at a time.
# Published prior
# → Recalculated prior
# → ARR
# → Multiple
# → Capitalization
# → Net cash

# Company equity: $55M
#     ↓ Allocate between share classes
# Entire Series B: $45.833M
#     ↓ Our fund owns 50% of Series B
# Our fund: $22.917M
def _attribution(dataset: Dataset, row: dict, quarter: Quarter) -> dict:
    """An ordered counterfactual bridge, not a claim of unique economic causation."""
    order = list(ATTRIBUTION_ORDER)
    result = {"available": False, "reason": None, "order": order, "steps": [],
              "total_change": None, "interaction": None,
              "caveat": "Order-dependent counterfactuals: interactions are assigned to the later step. "
                        "Capitalization effects hold valuation fixed. Effects subtract rounded state marks."}
    prior = row["prior_recalculation"]
    if row["status"] != "marked":
        result["reason"] = "Current mark unavailable"
    elif not prior["available"]:
        result["reason"] = prior["reason"]
    elif row["prior_quarter_mark"] is None:
        result["reason"] = "No eligible published prior-quarter mark"
    if result["reason"]:
        return result
    baseline = prior["calculation"]
    arr, multiple = Decimal(row["arr"]), Decimal(row["multiple"])
    old_arr, old_multiple = Decimal(baseline["arr"]), Decimal(baseline["multiple"])
    arr_only = _calculate(dataset, row["company"], quarter.previous, arr, old_multiple)
    both = _calculate(dataset, row["company"], quarter.previous, arr, multiple)
    current_cap = _calculate(dataset, row["company"], quarter, arr, multiple)
    current = _calculate(dataset, row["company"], quarter, arr, multiple, Decimal(row["net_cash"]))
    previous_value = Decimal(row["prior_quarter_mark"])
    steps = [{"step": order[0], "fund_value": str(previous_value), "effect": None, "calculation": None}]
    for label, calculation in zip(order[1:], (baseline, arr_only, both, current_cap, current)):
        value = Decimal(calculation["rounded_fund_position_value"])
        steps.append({"step": label, "fund_value": str(value), "effect": str(value - previous_value),
                      "calculation": calculation})
        previous_value = value
    total = previous_value - Decimal(row["prior_quarter_mark"])
    if (previous_value != Decimal(row["rounded_fund_position_value"])
            or sum(Decimal(step["effect"]) for step in steps[1:]) != total):
        raise MarksError(f"{row['company']}: attribution does not reconcile")
    # Separate isolated effects from the interaction, using the same prior cap.
    multiple_only = _calculate(dataset, row["company"], quarter.previous, old_arr, multiple)
    base_value = Decimal(baseline["rounded_fund_position_value"])
    arr_effect = Decimal(arr_only["rounded_fund_position_value"]) - base_value
    multiple_effect = Decimal(multiple_only["rounded_fund_position_value"]) - base_value
    joint = Decimal(both["rounded_fund_position_value"]) - base_value
    result.update(available=True, steps=steps, total_change=str(total), interaction={
        "basis": "Prior capitalization and zero net cash; differences of rounded marks",
        "arr_alone_effect": str(arr_effect), "multiple_alone_effect": str(multiple_effect),
        "joint_effect": str(joint), "interaction_effect": str(joint - arr_effect - multiple_effect),
        "multiple_only_calculation": multiple_only,
    })
    return result


def _company_report(dataset: Dataset, company: str, quarter: Quarter, as_of: date | None,
                    indexes: dict[str, ObservationIndex], overrides: list[dict]) -> dict:
    previous = quarter.previous
    effective = {item["field"]: Decimal(item["value"]) for item in overrides}
    chosen = {
        "arr": select_latest(indexes["kpis"].get(company, quarter), company, quarter, as_of,
                             label="ARR"),
        "prior_arr": select_latest(indexes["kpis"].get(company, previous), company, previous, as_of, label="prior ARR"),
        "multiple": select_latest(indexes["multiples"].get(company, quarter), company, quarter, as_of,
                                  label="multiple"),
        "prior_mark": select_latest(indexes["reported"].get(company, previous), company, previous, as_of, label="prior published mark"),
        "published_mark": select_latest(indexes["reported"].get(company, quarter), company, quarter, as_of, label="published mark"),
    }
    prior_multiple = select_latest(indexes["multiples"].get(company, previous), company, previous,
                                   as_of, label="prior multiple")
    arr_observation, multiple_observation = chosen["arr"].selected, chosen["multiple"].selected
    arr = effective.get("arr", arr_observation.value if arr_observation is not None else None)
    multiple = effective.get("multiple", multiple_observation.value if multiple_observation is not None else None)
    override_evidence = []
    for item in overrides:
        original = chosen[item["field"]].selected if item["field"] != "net_cash" else None
        original_value = (str(original.value) if original is not None else
                          "0" if item["field"] == "net_cash" else None)
        override_evidence.append({**item, "original_value": original_value,
                                  "original_input": original.to_dict() if original is not None else None})
    prior_recalculation = _prior_recalculation(dataset, company, previous, chosen, prior_multiple)
    missing = [name for name, value in (("arr", arr), ("multiple", multiple)) if value is None]
    if missing:
        reason = f"Missing eligible {', '.join(missing)} for {quarter}" + (f" as of {as_of}" if as_of else "")
        prior_mark = chosen["prior_mark"].selected
        return {
            "company": company, "status": "no_mark", "missing_inputs": missing,
            "errors": [{"code": "MISSING_CURRENT_INPUT", "message": reason}],
            "arr": str(arr) if arr is not None else None,
            "multiple": str(multiple) if multiple is not None else None,
            "net_cash": str(effective.get("net_cash", Decimal(0))),
            "enterprise_value": None, "equity_value": None, "ownership_pct": None,
            "fund_position_value": None, "rounded_fund_position_value": None,
            "prior_quarter_mark": str(prior_mark.value) if prior_mark is not None else None,
            "arr_growth_pct": None, "arr_growth_reason": reason,
            "mark_change_pct": None, "mark_change_reason": reason,
            "selected_inputs": {key: _optional(selection) for key, selection in chosen.items()},
            "input_history": {key: selection.history() for key, selection in chosen.items()},
            "cap_table": {}, "class_allocations": {}, "fund_contributions": {},
            "prior_recalculation": prior_recalculation, "arr_decline_history": [],
            "overrides": override_evidence,
            "warnings": _excluded_notes([(key.replace("_", " "), selection) for key, selection in chosen.items()]
                                         + [("prior-quarter multiple", prior_multiple)], as_of),
        }
    calculation = _calculate(dataset, company, quarter, arr, multiple, effective.get("net_cash", Decimal(0)))
    equity = Decimal(calculation["equity_value"])
    ownership = Decimal(calculation["ownership_pct"])
    fund_value = Decimal(calculation["fund_position_value"])
    rounded_fund = Decimal(calculation["rounded_fund_position_value"])
    cap = calculation["cap_table"]
    active, excluded = cap["active_series"], cap["excluded_series"]
    total_shares = cap["common_shares"] + sum(s["shares"] for s in active)
    fund_shares = sum(s["fund_shares"] for s in active)
    warnings = []

    def warn(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    for item in override_evidence:
        original_value = item["original_value"]
        warn("HYPOTHETICAL_OVERRIDE", f"Hypothetical {item['field']} override: "
             f"{original_value if original_value is not None else 'missing source value'} -> {item['value']}. "
             "Original inputs and published marks remain unchanged.")

    prior_arr = chosen["prior_arr"].selected
    decline_history = []
    decline_selections = []
    arr_growth = None
    if prior_arr is None:
        arr_reason = f"No eligible ARR for {previous}" if previous else "No earlier calendar quarter"
        warn("MISSING_PRIOR_ARR", arr_reason + "; ARR growth is unavailable.")
    elif prior_arr.value == 0:
        arr_reason = f"{previous} ARR is zero; percentage growth is undefined"
        warn("ZERO_PRIOR_ARR", arr_reason + ".")
    else:
        arr_reason = None
        arr_growth = _pct(arr, prior_arr.value)
        if abs(arr_growth) >= ARR_REVIEW_THRESHOLD * 100:
            warn("ARR_LARGE_CHANGE",
                 f"ARR changed from {prior_arr.value:,f} to {arr:,f} ({arr_growth:.3f}%); "
                 "confirm the reported amount and units. No correction was inferred.")
        if arr_growth < 0:
            warn("ARR_DECLINE", f"ARR declined {abs(arr_growth):.2f}% from {previous}.")
            decline_history.append(prior_arr.to_dict())
            latest, earlier_quarter = prior_arr, previous.previous
            while earlier_quarter is not None:
                earlier = select_latest(indexes["kpis"].get(company, earlier_quarter), company,
                                        earlier_quarter, as_of, label="decline-history ARR")
                decline_selections.append((f"{earlier_quarter} decline-history ARR", earlier))
                if earlier.selected is None or latest.value >= earlier.selected.value:
                    break
                latest = earlier.selected
                decline_history.append(latest.to_dict())
                earlier_quarter = earlier_quarter.previous
            if len(decline_history) >= 2:
                values = [Decimal(row["value"]) for row in reversed(decline_history)] + [arr]
                warn("ARR_CONSECUTIVE_DECLINES", f"ARR declined for {len(decline_history)} consecutive quarters "
                     "under the same knowledge cutoff (" + " -> ".join(f"{value:,f}" for value in values) + ").")

    for key, description in (("arr", "current ARR"), ("prior_arr", "prior-quarter ARR"),
                             ("multiple", "valuation multiple"), ("prior_mark", "prior published mark"),
                             ("published_mark", "published mark")):
        selection = chosen[key]
        revisions = selection.revisions
        if revisions:
            old, new = revisions[-1], selection.selected
            assert new is not None
            warn(key.upper() + "_REVISION",
                 f"Selected revised {description}: {old.value:,f} recorded {old.known_on} -> "
                 f"{new.value:,f} recorded {new.known_on}.")
        if selection.duplicates:
            warn("DUPLICATE_SOURCE_ROWS",
                 f"{description}: ignored {len(selection.duplicates)} identical duplicate row(s); "
                 "all row identities remain in input_history.")
    selected_streams = [(key.replace("_", " "), selection) for key, selection in chosen.items()]
    selected_streams += [("prior-quarter multiple", prior_multiple), *decline_selections]
    warnings.extend(_excluded_notes(selected_streams, as_of))
    analyst_note = dict(multiple_observation.source.cells).get("note", "").strip() if multiple_observation else ""
    if analyst_note:
        warn("ANALYST_NOTE", analyst_note)

    if previous is not None:
        previous_end = previous.end.isoformat()
        newly_issued = [s for s in active if s["issue_date"] > previous_end]
        if newly_issued:
            warn("NEW_SHARE_CLASS", "Classes issued during the quarter: " + ", ".join(
                f"{s['name']} ({s['issue_date']})" for s in newly_issued) + ".")
        prior_series = [s for s in active if s["issue_date"] <= previous_end]
        prior_total = cap["common_shares"] + sum(s["shares"] for s in prior_series)
        if prior_total > 0:
            prior_ownership = Decimal(sum(s["fund_shares"] for s in prior_series)) / prior_total * 100
            if abs(prior_ownership - ownership) > RECONCILIATION_TOLERANCE:
                warn("OWNERSHIP_CHANGE", f"Fund ownership changed from {prior_ownership:.2f}% "
                     f"to {ownership:.2f}% using share classes outstanding at each quarter-end.")
    if excluded:
        warn("FUTURE_SERIES_EXCLUDED", "Excluded classes issued after quarter-end: " +
             ", ".join(s["name"] for s in excluded) + ".")
    pro_rata = equity * fund_shares / total_shares
    if abs(fund_value - pro_rata) >= DOLLAR:
        warn("PREFERENCE_EFFECT", f"Preferred rights produce fund value ${rounded_fund:,.0f} versus "
             f"${pro_rata.quantize(DOLLAR):,.0f} from simple ownership times equity value.")

    prior_mark = chosen["prior_mark"].selected
    mark_change = None
    if prior_mark is None:
        mark_reason = f"No eligible published mark for {previous}" if previous else "No earlier calendar quarter"
        warn("MISSING_PRIOR_MARK", mark_reason + "; mark change is unavailable.")
    elif prior_mark.value == 0:
        mark_reason = f"{previous} published mark is zero; percentage change is undefined"
        warn("ZERO_PRIOR_MARK", mark_reason + ".")
    else:
        mark_reason = None
        mark_change = _pct(rounded_fund, prior_mark.value)
    if prior_recalculation["delta"] is not None and Decimal(prior_recalculation["delta"]) != 0:
        warn("PRIOR_MARK_RECALCULATED",
             f"{previous} recalculated at the same knowledge cutoff: "
             f"${Decimal(prior_recalculation['calculation']['rounded_fund_position_value']):,.0f} versus "
             f"published ${prior_mark.value:,.0f}; delta ${Decimal(prior_recalculation['delta']):+,.0f}. "
             "The published mark remains the comparison baseline.")
    published = chosen["published_mark"].selected
    if published is not None and rounded_fund != published.value:
        message = (f"Calculated {quarter} mark ${rounded_fund:,.0f} differs from published "
                   f"mark ${published.value:,.0f} reported {published.known_on}.")
        if as_of == published.known_on and not overrides:
            raise MarksError(f"{company}: historical reproduction failed. {message}")
        suffix = (" This company has hypothetical overrides." if overrides else
                  " Later eligible revisions may restate the calculation.")
        warn("PUBLISHED_MARK_DIFFERENCE", message + suffix)

    return {
        "company": company, "status": "marked", "missing_inputs": [], "errors": [], **calculation,
        "arr_growth_pct": str(arr_growth) if arr_growth is not None else None,
        "arr_growth_reason": arr_reason,
        "prior_quarter_mark": str(prior_mark.value) if prior_mark is not None else None,
        "mark_change_pct": str(mark_change) if mark_change is not None else None,
        "mark_change_reason": mark_reason,
        "selected_inputs": {key: _optional(selection) for key, selection in chosen.items()},
        "input_history": {key: selection.history() for key, selection in chosen.items()},
        "prior_recalculation": prior_recalculation, "arr_decline_history": decline_history,
        "overrides": override_evidence,
        "warnings": warnings,
    }


def build_report(dataset: Dataset, quarter: Quarter, as_of: date | None = None,
                 overrides=None, *, include_attribution: bool = False) -> dict:
    """Keep missing-input companies visible; reject invalid data and arithmetic."""
    if type(include_attribution) is not bool:
        raise MarksError("include_attribution must be a boolean")
    if as_of is not None and as_of < quarter.end:
        raise MarksError(f"as-of {as_of} precedes {quarter} quarter-end {quarter.end}")
    normalized = normalize_overrides(overrides, dataset.cap_tables)
    by_company = {}
    for item in normalized:
        by_company.setdefault(item["company"], []).append(item)
    try:
        with localcontext(DECIMAL_CONTEXT):
            indexes = {"kpis": ObservationIndex(dataset.kpis),
                       "multiples": ObservationIndex(dataset.multiples),
                       "reported": ObservationIndex(dataset.reported)}
            companies = [_company_report(dataset, company, quarter, as_of, indexes, by_company.get(company, []))
                         for company in sorted(dataset.cap_tables)]
            for row in companies:
                row["attribution"] = _attribution(dataset, row, quarter) if include_attribution else None
    except DecimalException as exc:
        raise MarksError(f"valuation arithmetic failed: {type(exc).__name__}") from exc
    marked_count = sum(row["status"] == "marked" for row in companies)
    return {"quarter": str(quarter), "quarter_end": quarter.end.isoformat(),
            "as_of": as_of.isoformat() if as_of is not None else None,
            "cutoff_mode": "as_of" if as_of is not None else "latest_supplied",
            "hypothetical": bool(normalized), "overrides": normalized,
            "include_attribution": include_attribution,
            "status": "complete" if marked_count == len(companies) else "incomplete",
            "marked_company_count": marked_count,
            "conventions": dict(CONVENTIONS), "companies": companies}
