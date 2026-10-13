# Running quarterly marks

Use Python 3.11 or later; no external packages are needed. From the folder
containing `marks.py`, try these six demos:

```sh
python marks.py --quarter 2026Q2 --as-of 2026-08-14
python marks.py --quarter 2026Q3
python marks.py --quarter 2026Q3 --explain "Pinewell"
python marks.py --quarter 2026Q3 --override "Pinewell.arr=13000000"
python marks.py --quarter 2026Q3 --attribution
python marks.py --replay sample_receipts/q2.json
```

Q2 reproduces the published marks. Q3 uses the latest supplied records, including
October 20. Cutoffs are inclusive; capitalization is measured at quarter-end.

The Pinewell scenario produces $9,166,667 and keeps the original $13,000 observation.
Repeat `--override` for `Company.arr`, `Company.multiple` or `Company.net_cash`.
Net cash adds to enterprise value to produce the equity sent to the waterfall;
negative resulting equity is rejected. Scenario reports and replays say hypothetical.

Attribution shows its order, intermediate marks and interaction effects. Missing
prior inputs make the bridge unavailable. It explains counterfactual changes,
not unique causes. Replay retains the original attribution request and overrides.

A missing current input leaves a `NO MARK` row and an incomplete report. Add
`--strict` to exit 2 after saving and printing it, including on replay. Malformed
or conflicting data still fails the run. No partial portfolio total is shown.

Every report gets a receipt before printing. Inputs default to `data/` beside the
script; receipts go to `audits/` in your working directory. Use `--data-dir` and
`--audit-dir` to change them. Copied CSVs can be edited directly: no manifest ships.
An external manifest is always verified when present; `--require-manifest` requires one.

`--format json` writes structured output to stdout; diagnostics go to stderr.
Compare saved runs with:

```sh
python marks.py --compare FIRST_RECEIPT.json SECOND_RECEIPT.json --format json
```

Replace those paths with receipts printed by earlier runs. Replay uses captured
sources and writes a new receipt. Minor/patch Python differences warn and require
an exact report match; calculation code, implementation and major version must match.
The bundled samples record CPython 3.11.0. Older receipt schemas need their saved engine.

Layout: `quarterly_marks/` contains the implementation, `tests/` the checks,
`sample_receipts/` two examples. Read `NOTES.md` for decisions and `DATA_MODEL.md`
for the database proposal.

```sh
python -m unittest discover -s tests -v
```

Exit codes: 0 completed; 2 input/usage or strict incompleteness; 3 audit/replay;
1 unexpected error; 130 interruption.
