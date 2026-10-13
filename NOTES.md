# What finance should know

| Company | Finding |
| --- | --- |
| Halcyon Metrics | Q2 ARR changed from $20m to $18.5m on September 10, after August's report. Recalculated Q2 is $21,907,895 versus $23,684,211 published: a $1,776,316 decrease between rounded marks. Q3 ARR growth uses corrected ARR; mark change keeps the published baseline. |
| Orbitra | Q2's multiple changed from 8.0 to 8.5 on September 5. Recalculated Q2 is $28,333,333 versus $26,666,667 published: a $1,666,666 increase between rounded marks. Series C reduces share-count ownership from 16.67% to 14.63%; fund shares are unchanged. At fixed $246m Q3 equity, the old cap table gives $41m and the new one $36m: a $5m capitalization effect. |
| Pinewell | The supplied Q3 ARR is $13,000 after $12.5m. We calculate that value: its $91,000 equity goes to senior Series B, leaving the fund $0. Confirm the amount and units before approval. A hypothetical $13m ARR produces $9,166,667; it does not correct the source. |
| Kestrel Labs | ARR declined $16m → $13m → $10m. The latest Q3 multiple is 5.5, revised October 20. From Q2's 8.0 multiple, changing ARR alone gives $80m equity; changing the multiple alone gives $71.5m. Either preserves the $25m fund mark. Together they give $55m, below $60m of equal-priority claims: $25m × $55m / $60m = $22,916,667, rounded. The loss is an interaction assigned to the later step in a sequential bridge. |

`--quarter 2026Q2 --as-of 2026-08-14` reproduces all four published marks.
`--quarter 2026Q3` selects latest supplied inputs. Values are whole USD dollars:

| Company | Q2 as of August 14 | Q3 latest supplied |
| --- | ---: | ---: |
| Halcyon Metrics | 23,684,211 | 22,105,263 |
| Orbitra | 26,666,667 | 36,000,000 |
| Pinewell | 14,833,333 | 0 |
| Kestrel Labs | 25,000,000 | 22,916,667 |

## Five decisions

1. **Separate quarter-end from knowledge date.** Include classes issued by quarter-end. Select inputs known by the inclusive cutoff; without `--as-of`, use all records, including October 20, regardless of today's date. File order never determines a revision. Identical rows are harmless; same-date differences in values or metadata require resolution. [Selection logic](quarterly_marks/selection.py).

2. **Keep published comparisons stable.** Growth uses prior ARR selected under the same cutoff. Mark change uses the published prior mark, even when a recalculation differs. Recalculation does not establish that an LP report was formally restated. Missing comparisons and zero denominators show `N/A` with a reason.

3. **Apply the supplied policy exactly.** Default equity equals ARR times multiple. The supplied waterfall allocates equity; sum each class's payout times the fund's fraction of that class. Ownership percentage is not payout percentage. Use a local 60-digit Decimal context; round only the final fund value half-up to dollars. Half-up is our stated tie convention. Preferences cushion losses without guaranteeing a floor. [Calculation logic](quarterly_marks/valuation.py).

4. **Keep incomplete work visible.** Missing current ARR or multiple produces a `NO MARK` row and an incomplete report; other companies still calculate. No missing mark becomes zero, and no portfolio total is presented. Default exit is 0; `--strict` saves and prints the report, then exits 2. Malformed files, conflicting observations, invalid holdings and calculation failures stop the run. Valid but suspicious amounts calculate; the flag for ARR changes of at least 50% is a review heuristic.

5. **Explain changes with stated assumptions.** Attribution walks from published prior to recalculated prior, then changes ARR, multiple, capitalization and net cash in that order. Effects subtract consecutive rounded marks and sum to the displayed change. Isolated ARR/multiple calculations expose their interaction. These counterfactuals are order-dependent. Missing baselines make attribution unavailable. Overrides are hypothetical, retain original evidence and replay unchanged; negative resulting equity is rejected.

## Limits

- Common shares, fund holdings and existing preferred terms are assumed static; only preferred issuance is dated. No fund common holding is supplied. Amounts are assumed to be USD dollars. The proposed database model describes fuller history.
- Use one consistent export. The loader validates values, relationships and concurrent edits, but cannot establish that files belong to one approved export. No manifest ships; if supplied, it verifies expected files and content hashes, not business accuracy.
- Receipts preserve source text, selections and calculations; replay checks the complete report. Hashes provide integrity checks, not independent proof of authenticity or approval. [Audit logic](quarterly_marks/audit.py). Original assignment files remain unchanged; database deployment and LP publication are outside this CLI.
