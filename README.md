# Take-Home: Quarterly Marks

**Time:** 1–2 hours. Please don't spend more.
**AI tools:** Encouraged. Use Claude, Cursor, Copilot, or whatever you like. We care about the design decisions and whether you understand the output.

## Scenario

It's mid-October 2026. You're the engineer supporting a venture fund's finance team. Each quarter they value ("mark") every portfolio company, and those marks go to investors (LPs) and auditors.

Today their process is spreadsheets. You're building the first version of the system that replaces them. Start by reading `policy.md`. It explains the calculation and where the data comes from.

## What's here

```
policy.md          how the finance team marks the portfolio (read first)
waterfall.py       provided: splits company value across share classes. Use it; don't rewrite it.
data/
  kpis.csv               ARR submitted by each company
  valuation_inputs.csv   EV/ARR multiples entered by finance analysts
  cap_tables.json        each company's preferred stock series
  q2_marks.csv           Q2 2026 marks as reported to LPs
```

## What to build

A Python script, `marks.py`, that does three things:

1. **Produces this quarter's marks.**
   `python marks.py --quarter 2026Q3` prints a table with one row per company: ARR, QoQ growth, multiple, enterprise value, fund ownership %, fund position value, prior-quarter mark, and % change. Print anything a reviewer of these marks should know.

2. **Reproduces history.**
   `python marks.py --quarter 2026Q2 --as-of 2026-08-14` must reproduce the Q2 marks in `q2_marks.csv` exactly. Auditors will ask for this.

3. **Leaves an audit trail.**
   Each run writes a file (JSON is fine) that records, for every mark, exactly which inputs produced it.

## Also include

- **`DATA_MODEL.md`** (1 page max): how you would store this data in a real database (Postgres, say) once the finance team is entering it through an app instead of CSVs. Cover how corrections work, and how you'd answer "what did we know on date X?"
- **`NOTES.md`**: judgment calls you made and anything in the data you think the finance team should hear about.
- **Tests** for the parts most likely to break.

Keep dependencies minimal. The standard library is enough, and pandas is fine too.

## Submitting

Send a zip or a Git repo link. In the follow-up interview we'll walk through your output and data model together and make a small change live. Be ready to explain every number and every design choice.
