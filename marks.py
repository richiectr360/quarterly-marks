#!/usr/bin/env python3
"""Calculate, explain, reproduce, and compare auditable quarterly marks."""
from __future__ import annotations

'''
When you run: python3 marks.py --quarter 2026Q3

Python calls main().
The number returned by main() becomes the command’s exit code:

0 = completed
2 = input/usage error, or an incomplete report with --strict
3 = audit/replay error
1 = unexpected error
130 = interrupted

The financial calculations happen in valuation.py, which is called by main() after loading and validating the inputs.

parser() reads what you requested -> args = parser().parse_args(argv) -> python3 marks.py --quarter 2026Q3 --explain "Kestrel Labs"

args.quarter = "2026Q3"
args.explain = "Kestrel Labs"
args.format = "text"
args.audit_dir = Path("audits")

| Action | Meaning |
|---|---|
| `--quarter` | Calculate a report |
| `--replay` | Reproduce a saved run |
| `--compare` | Compare two saved runs |

For a normal calculation, the actual sequence is:
Parse quarter and optional cutoff
        ↓
Read and validate source files
        ↓
Parse any hypothetical overrides
        ↓
Build the financial report
        ↓
Prepare text output, if requested
        ↓
Create the audit receipt in memory

“marks.py handles the command-line workflow. It validates the request, delegates calculation or replay,
 saves the audit evidence before printing success, and returns an exit code that other programs can interpret.”

 6. main() saves the receipt before printing the result
This ordering is deliberate:
path = write_receipt(receipt, args.audit_dir)
Only after saving succeeds does it print:
print(text)
The principle is:
A successfully displayed valuation should have a saved audit record.

If the audit directory cannot be written, the command reports an audit error instead of printing a successful valuation.
For JSON output, it prints a structured object containing:
run_id
audit_file
report or comparison
Diagnostic messages—including the receipt’s location—go to stderr, keeping JSON on stdout usable by another program.
7. It handles incomplete reports
Suppose Halcyon’s current ARR is missing.
The report can still contain:
Halcyon: NO MARK
Other companies: calculated results
Report status: INCOMPLETE
The receipt and report are saved and displayed first.
Then:
if report_is_incomplete:
    if args.strict:
        return 2

return 0
So:
- Without --strict: the incomplete report finishes with exit 0.
- With --strict: the same report finishes with exit 2.
This allows a person to inspect partial results while letting an automated pipeline reject incompleteness.
'''


import argparse
import json
import sys
from pathlib import Path

from quarterly_marks import __version__
from quarterly_marks.audit import (
    compare_receipts, create_comparison_receipt, create_receipt,
    load_receipt, replay_receipt, write_receipt,
)
from quarterly_marks.domain import AuditError, MarksError, Quarter, parse_date
from quarterly_marks.inputs import load_dataset, parse_sources
from quarterly_marks.reporting import render_comparison, render_explanation, render_report
from quarterly_marks.scenarios import parse_overrides
from quarterly_marks.valuation import build_report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    action = result.add_mutually_exclusive_group(required=True)
    action.add_argument("--quarter", metavar="YYYYQn", help="quarter to calculate")
    action.add_argument("--replay", type=Path, metavar="RECEIPT", help="verify and reproduce a saved run")
    action.add_argument("--compare", nargs=2, type=Path, metavar=("FIRST", "SECOND"), help="compare two saved valuation runs")
    result.add_argument("--as-of", metavar="YYYY-MM-DD", help="inclusive knowledge cutoff; default is latest supplied")
    result.add_argument("--data-dir", type=Path, help="source directory; default is data beside this script")
    result.add_argument("--audit-dir", type=Path, default=Path("audits"), help="receipt directory; default: ./audits")
    result.add_argument("--explain", metavar="COMPANY", help="append a company's calculation and evidence (text output)")
    result.add_argument("--format", choices=("text", "json"), default="text", help="stdout format; diagnostics use stderr")
    result.add_argument("--version", action="version", version=f"Quarterly Marks {__version__}")
    result.add_argument("--require-manifest", action="store_true", help="reject unsealed input directories")
    result.add_argument("--override", action="append", default=[], metavar="Company.field=value",
                        help="hypothetical arr, multiple, or net_cash; repeat for different fields")
    result.add_argument("--attribution", action="store_true", help="explain mark changes using an ordered bridge")
    result.add_argument("--strict", action="store_true", help="exit 2 after saving and displaying an incomplete report")
    return result


def _check_options(args: argparse.Namespace) -> None:
    if args.as_of is not None and args.quarter is None:
        raise MarksError("--as-of can only be used with --quarter")
    if args.data_dir is not None and args.quarter is None:
        raise MarksError("--data-dir requires --quarter")
    if args.require_manifest and args.quarter is None:
        raise MarksError("--require-manifest requires --quarter")
    if args.override and args.quarter is None:
        raise MarksError("--override requires --quarter; replay uses the saved overrides")
    if args.attribution and args.quarter is None:
        raise MarksError("--attribution requires --quarter; replay uses the saved attribution request")
    if args.strict and args.compare:
        raise MarksError("--strict applies to valuation reports, not comparisons")
    if args.explain is not None and (args.compare or args.format == "json"):
        raise MarksError("--explain requires a valuation report in --format text")


def _render_valuation(report: dict, explain: str | None) -> str:
    text = render_report(report)
    if explain is not None:
        selected = next((mark for mark in report["companies"] if mark["company"] == explain), None)
        if selected is None:
            choices = ", ".join(mark["company"] for mark in report["companies"])
            raise MarksError(f"unknown company {explain!r}; choose one of: {choices}")
        text += "\n\n" + render_explanation(selected)
    return text


def _run(args: argparse.Namespace, diagnostics: list[str]) -> tuple[dict, str, str]:
    """Prepare a complete result and its receipt before publishing anything."""
    if args.quarter is not None:
        quarter = Quarter.parse(args.quarter)
        as_of = parse_date(args.as_of, "--as-of") if args.as_of is not None else None
        data = load_dataset(args.data_dir or Path(__file__).resolve().parent / "data",
                            require_manifest=args.require_manifest)
        report = build_report(data, quarter, as_of, overrides=parse_overrides(args.override),
                              include_attribution=args.attribution)
        text = _render_valuation(report, args.explain) if args.format == "text" else ""
        return create_receipt(data, report), text, "report"
    if args.compare:
        first, second = [load_receipt(path, require_compatible=False) for path in args.compare]
        comparison = compare_receipts(first, second)
        text = render_comparison(comparison) if args.format == "text" else ""
        return create_comparison_receipt(first, second, comparison), text, "comparison"
    saved, reproduced = replay_receipt(args.replay)
    if saved["operation"] == "compare":
        if args.strict:
            raise MarksError("--strict applies to valuation reports, not comparisons")
        if args.explain is not None:
            raise MarksError("--explain is unavailable for comparison receipts")
        receipt = create_comparison_receipt(saved["left_receipt"], saved["right_receipt"], reproduced)
        text = "Comparison replay verified.\n\n" + render_comparison(reproduced) if args.format == "text" else ""
        return receipt, text, "comparison"
    data = parse_sources({name: source["text"] for name, source in saved["sources"].items()})
    text = ("Replay verified: captured inputs reproduce the saved report.\n\n" + _render_valuation(reproduced, args.explain)
            if args.format == "text" else "")
    receipt = create_receipt(data, reproduced, operation="replay", parent_run_ids=[saved["run_id"]])
    recorded, running = saved["engine"].get("runtime"), receipt["engine"]["runtime"]
    if recorded is not None and recorded != running:
        diagnostics.append(
            f"WARNING [RUNTIME_DIFFERENCE]: Recorded with {recorded['implementation']} {recorded['python']}; "
            f"replayed with {running['implementation']} {running['python']}. The full report reproduced exactly.")
    return receipt, text, "report"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        _check_options(args)
        diagnostics = []
        receipt, text, result_key = _run(args, diagnostics)
        # Save evidence first. No successful mark is printed without its receipt.
        path = write_receipt(receipt, args.audit_dir)
        for diagnostic in diagnostics:
            print(diagnostic, file=sys.stderr)
        if args.format == "json":
            output = {"run_id": receipt["run_id"], "audit_file": str(path.resolve()), result_key: receipt[result_key]}
            if args.replay:
                output["replayed_from"] = str(args.replay)
            print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False))
        else:
            print(text)
        print(f"Audit saved: {path.resolve()}", file=sys.stderr)
        if result_key == "report" and receipt["report"]["status"] == "incomplete":
            print("INCOMPLETE_REPORT: missing current inputs; no portfolio total is available.", file=sys.stderr)
            if args.strict:
                return 2
        return 0
    except AuditError as exc:
        print(f"AUDIT_ERROR: {exc}", file=sys.stderr)
        return 3
    except MarksError as exc:
        print(f"INPUT_ERROR: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        print("INTERRUPTED: run did not finish; any published receipt remains available.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
