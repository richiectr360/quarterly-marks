# Quarterly marks: database model

Proposed Postgres design, following the CLI's separation of company facts, fund
assumptions, ownership, results and publications. Today the CLI uses files and receipts.

| Tables | One record represents / connection to this CLI |
| --- | --- |
| `companies`, `funds`, `share_classes` | Stable IDs; each class belongs to a company. Names are labels, not keys. |
| `observation_series`, `observation_revisions` | A company/metric/period/unit/currency stream and its immutable revisions; stores ARR from `kpis.csv`. |
| `valuation_input_series`, `valuation_input_revisions` | A fund/company/period/policy/assumption stream and its revisions; stores multiples from `valuation_inputs.csv`. |
| `capital_events`, `capital_revisions` | A company event and revised complete capital snapshots, with class-keyed quantities and rights from `cap_tables.json`. |
| `holding_events`, `holding_revisions` | A fund/company event and revised complete snapshots of holdings by class. Separate `fund_shares` from company capital: transfers do not change outstanding shares. |
| `source_versions`, `import_batches` | Immutable source revisions and validated import membership; preserve payload/hash, author and row/JSON locator. |
| `valuation_runs`, `results` | A fund/quarter calculation and one result per company, including unavailable status. Pin input revisions, prior published line, policy/code/runtime, intermediate values and rounding, as receipts do today. |
| `published_reports`, `report_lines` | A fund/quarter/report version and one line per company. Native lines reference approved results; imported `q2_marks.csv` lines retain evidence without an invented original run or approver. |

**Corrections preserve history.** Halcyon's Q2 ARR: $20m known July 15, corrected
to $18.5m September 10. August 14 selects $20m; August's published mark stays unchanged.
Append revisions with predecessor, actor and reason. Unique stream/event revision
numbers and expected-predecessor locking reject competing edits. Withdrawals append
voids; wrong-quarter corrections void the old stream and create the correct one
atomically. Duplicate source identity/revision pairs are idempotent; changed content
under the same pair is a conflict.

**Historical selection.** Period/effective date, finance's `known_at` and system
`ingested_at` are distinct. Retain evidence and date precision; do not invent historical
timestamps. Resolve revisions before checking voids or effective dates. For one
company's capital snapshots:

```sql
WITH known AS (
  SELECT r.*, row_number() OVER (
    PARTITION BY event_id ORDER BY known_at DESC, revision_no DESC
  ) AS n
  FROM capital_revisions r
  WHERE tenant_id = :tenant AND company_id = :company
    AND known_at <= :cutoff
), eligible AS (
  SELECT * FROM known WHERE n = 1 AND NOT is_void
    AND effective_on <= :quarter_end
)
SELECT * FROM eligible
WHERE effective_on = (SELECT max(effective_on) FROM eligible);
```

Require one state; same-date event ties need business ordering or review. Holdings
use this pattern scoped to fund. Observations select the latest eligible revision
per stream, then interpret its void; never resurrect an older value. Correcting an
earlier complete snapshot requires reviewing later snapshots.

**Integrity and publication.** Enforce tenant/company/fund-consistent foreign keys,
exact `numeric` amounts and bounded integer shares. Validate periods, units, currencies
and holdings against capital. Read a consistent committed snapshot; save runs atomically.
Replay pinned inputs with preserved executable versions. Publications never change;
restatements create versions. Ownership history and approvals are proposed additions.
