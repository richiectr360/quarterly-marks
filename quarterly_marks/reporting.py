"""Human-readable views of the same immutable results saved in audit receipts."""

from __future__ import annotations

import json
from decimal import Decimal, localcontext

from .domain import DECIMAL_CONTEXT


def money(value: str | None, *, places: int = 0) -> str:
    if value is None:
        return "N/A"
    with localcontext(DECIMAL_CONTEXT):
        number = Decimal(value).quantize(Decimal(1).scaleb(-places))
        return f"${number:,.{places}f}"


def percent(value: str | None) -> str:
    if value is None:
        return "N/A"
    with localcontext(DECIMAL_CONTEXT):
        number = Decimal(value).quantize(Decimal("0.01"))
        if number == 0:
            number = abs(number)
        return f"{number:,.2f}%"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i])
                         for i, cell in enumerate(cells))
    return "\n".join([line(headers), "  ".join("-" * width for width in widths),
                       *(line(row) for row in rows)])


def render_report(report: dict) -> str:
    cutoff = report["as_of"] or "latest supplied observations (no clock-based cutoff)"
    label = "HYPOTHETICAL QUARTERLY MARKS" if report["hypothetical"] else "QUARTERLY MARKS"
    lines = [f"{label} | {report['quarter']} | quarter-end {report['quarter_end']}",
             f"Knowledge cutoff: {cutoff}",
             "Amounts in assumed USD; ownership is share-count ownership, not the payout percentage.", ""]
    rows = []
    for mark in report["companies"]:
        rows.append([mark["company"], money(mark["arr"]), percent(mark["arr_growth_pct"]),
                     f"{Decimal(mark['multiple']):f}x" if mark["multiple"] is not None else "N/A",
                     money(mark["enterprise_value"]), money(mark["equity_value"]),
                     percent(mark["ownership_pct"]),
                     money(mark["rounded_fund_position_value"]) if mark["status"] == "marked" else "NO MARK",
                     money(mark["prior_quarter_mark"]), percent(mark["mark_change_pct"])])
    company_header = "Company (HYPOTHETICAL)" if report["hypothetical"] else "Company"
    lines.append(_table([company_header, "ARR", "ARR QoQ", "Multiple", "Enterprise value", "Equity value", "Ownership",
                         "Fund value", "Prior published", "Mark change"], rows))
    if report["status"] == "incomplete":
        lines.append(f"INCOMPLETE: {report['marked_company_count']} of {len(report['companies'])} companies marked; no portfolio total.")
    if report["hypothetical"]:
        lines.append("Scenario overrides: " + "; ".join(
            f"{item['company']}.{item['field']}={item['value']}" for item in report["overrides"]))
    lines.extend(["", "Comparisons: corrected prior ARR; prior fund values remain as published.",
                  "Common shares and fund holdings are assumed unchanged except for dated issuances.",
                  "Calculated results require finance review; warnings do not change source values."])
    warnings = [(mark["company"], warning) for mark in report["companies"] for warning in mark["warnings"]]
    if warnings:
        lines.extend(["", "REVIEW NOTES"])
        for company, warning in warnings:
            lines.append(f"- {company} [{warning['code']}]: {warning['message']}")
    for mark in report["companies"]:
        for error in mark["errors"]:
            lines.append(f"- {mark['company']} ERROR [{error['code']}]: {error['message']}")
    if report["include_attribution"]:
        lines.extend(["", "MARK CHANGE ATTRIBUTION"])
        for mark in report["companies"]:
            bridge = mark["attribution"]
            lines.extend(["", mark["company"], "Order: " + " -> ".join(bridge["order"])])
            if not bridge["available"]:
                lines.append("Unavailable: " + bridge["reason"])
                continue
            lines.append(_table(["Step", "Fund mark", "Effect"], [
                [step["step"], money(step["fund_value"]), money(step["effect"])] for step in bridge["steps"]]))
            lines.append("Total change from published prior: " + money(bridge["total_change"]))
            interaction = bridge["interaction"]
            lines.append("Holding prior capitalization and zero net cash fixed: ARR alone " +
                         money(interaction["arr_alone_effect"]) + "; multiple alone " +
                         money(interaction["multiple_alone_effect"]) + "; joint change " +
                         money(interaction["joint_effect"]) + "; interaction " + money(interaction["interaction_effect"]) + ".")
            lines.append(bridge["caveat"])
    return "\n".join(lines)


def _source(label: str, observation: dict | None) -> str:
    if observation is None:
        return f"{label}: not supplied or not available by the cutoff."
    source = observation["source"]
    return (f"{label}: {observation['value']} | {observation['quarter']} | "
            f"known {observation['known_on']} | {source['filename']}:{source['row_number']}\n"
            f"  Original fields: {json.dumps(source['raw'], ensure_ascii=False, sort_keys=True)}")


def render_explanation(mark: dict) -> str:
    label = "HYPOTHETICAL EXPLANATION" if mark["overrides"] else "EXPLANATION"
    lines = [f"{label} | {mark['company']}", "", "SELECTED SOURCE RECORDS"]
    labels = [("arr", "Current ARR"), ("prior_arr", "Prior ARR"), ("multiple", "Multiple"),
              ("prior_mark", "Prior published mark"), ("published_mark", "This quarter's published reference")]
    for key, label in labels:
        lines.append(_source(label, mark["selected_inputs"].get(key)))
    if mark["overrides"]:
        lines.extend(["", "HYPOTHETICAL OVERRIDES"])
        for item in mark["overrides"]:
            original = item["original_value"] if item["original_value"] is not None else "not supplied"
            lines.append(f"{item['field']}: {original} -> {item['value']} (original source retained)")
    if mark["status"] == "no_mark":
        lines.extend(["", "NO MARK"])
        lines.extend("ERROR: " + error["message"] for error in mark["errors"])
        return "\n".join(lines)
    cap = mark["cap_table"]
    lines.extend(["", "CAPITALIZATION AT QUARTER-END",
                  f"Source: cap_tables.json, company key {mark['company']!r}",
                  f"Common shares (including option pool): {cap['common_shares']:,}"])
    series_rows = [[s["name"], f"{s['shares']:,}", money(s["issue_price"], places=2),
                    s["issue_date"], str(s["seniority"]), f"{s['fund_shares']:,}"]
                   for s in cap["active_series"]]
    if series_rows:
        lines.append(_table(["Class", "Shares", "Issue price", "Issue date", "Seniority", "Fund shares"], series_rows))
    for excluded in cap["excluded_series"]:
        lines.append(f"Excluded: {excluded['name']} issued {excluded['issue_date']} after quarter-end.")
    lines.extend(["", "CALCULATION",
                  f"Enterprise value = {money(mark['arr'], places=2)} x {mark['multiple']} = "
                  f"{money(mark['enterprise_value'], places=2)}",
                  f"Equity value = enterprise value + net cash = {money(mark['enterprise_value'], places=2)} "
                  f"+ {money(mark['net_cash'], places=2)} = {money(mark['equity_value'], places=2)}.",
                  "The supplied policy uses zero net cash; an explicit net_cash override is hypothetical.",
                  "Higher seniority is paid first; equal ranks share a shortfall proportionally."])
    payouts = [[name, money(value, places=2), money(mark["fund_contributions"].get(name, "0"), places=2)]
               for name, value in sorted(mark["class_allocations"].items())]
    lines.append(_table(["Class", "Class allocation", "Fund contribution"], payouts))
    lines.extend(["Class amounts above are displayed to cents; the audit retains full calculation precision.",
                  "Each fund contribution = class allocation x fund shares / class shares.",
                  f"Fund value before dollar rounding: {mark['fund_position_value']}",
                  f"Calculated fund value (whole dollars): {money(mark['rounded_fund_position_value'])}",
                  f"Share-count ownership: {percent(mark['ownership_pct'])}",
                  f"ARR growth: {percent(mark['arr_growth_pct'])}",
                  f"Change from prior published mark: {percent(mark['mark_change_pct'])}"])
    for key in ("arr_growth_reason", "mark_change_reason"):
        if mark.get(key):
            lines.append(f"Comparison note: {mark[key]}")
    if mark["warnings"]:
        lines.extend(["", "REVIEW NOTES"])
        lines.extend(f"- [{w['code']}] {w['message']}" for w in mark["warnings"])
    return "\n".join(lines)


def render_comparison(comparison: dict) -> str:
    def request(value: dict) -> str:
        label = " / HYPOTHETICAL" if value["hypothetical"] else ""
        return f"{value['quarter']} / {value['as_of'] or 'latest supplied'}{label}"

    def value_text(value: object, field: str = "") -> str:
        if value is None:
            return "N/A"
        if field.endswith("_pct"):
            return percent(str(value))
        if field in {"arr", "enterprise_value", "net_cash", "equity_value", "fund_position_value", "rounded_fund_position_value", "prior_quarter_mark"} or field.startswith(("class_allocations.", "fund_contributions.")):
            return money(str(value), places=2 if field == "fund_position_value" or "." in field else 0)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    lines = ["RUN COMPARISON", "Observed differences; no causal attribution or revaluation is implied.",
             f"First:  {comparison['first_run_id']} | {request(comparison['first_request'])}",
             f"Second: {comparison['second_run_id']} | {request(comparison['second_request'])}",
             f"Companies with changes: {comparison['changed_company_count']}"]
    for key, label in (("added_companies", "Added companies"), ("removed_companies", "Removed companies"),
                       ("changed_source_files", "Changed source files")):
        if comparison[key]:
            lines.append(f"{label}: {', '.join(comparison[key])}")
    for key, label in (("request_changes", "Request"), ("report_changes", "Report conventions"), ("engine_changes", "Calculation engine")):
        for change in comparison.get(key, []):
            lines.append(f"{label}: {change['field']}: {value_text(change['before'])} -> {value_text(change['after'])}")
    if not comparison["has_changes"]:
        lines.append("No changes to captured inputs, calculation conventions, or results.")
    for change in comparison["changes"]:
        lines.extend(["", change["company"]])
        if change["financial_changes"]:
            rows = [[entry["field"], value_text(entry["before"], entry["field"]),
                     value_text(entry["after"], entry["field"])] for entry in change["financial_changes"]]
            lines.append(_table(["Result", "Before", "After"], rows))
        for key, label in (("selected_input_changes", "Input evidence"), ("cap_table_changes", "Cap table")):
            for entry in change[key]:
                lines.append(f"- {label}, {entry['field']}: {value_text(entry['before'])} -> {value_text(entry['after'])}")
        if change["warning_changes"]:
            lines.append("- Review warnings changed; exact before/after warnings are retained in the comparison receipt.")
    return "\n".join(lines)
