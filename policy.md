# How the Finance Team Marks the Portfolio

Every quarter, the finance team values ("marks") each portfolio company. The marks go to LPs in the quarterly report and to the auditors at year-end, so every number has to be explainable and reproducible later.

## The calculation

For each company and quarter:

1. **Enterprise value** = ARR at quarter-end × the EV/ARR multiple the finance team chose for that company and quarter (`valuation_inputs.csv`).
2. **Equity value** = enterprise value. Ignore cash and debt for this exercise.
3. **Allocate** equity value across share classes using `waterfall.allocate()`, with the cap table **as it stood at quarter-end**.
4. **Fund position value** = value allocated to the shares the fund holds (`fund_shares`).

## How the data arrives

- **KPIs** (`kpis.csv`): portfolio companies email their numbers after each quarter closes, and someone keys them in. Companies sometimes send corrections later.
- **Valuation inputs** (`valuation_inputs.csv`): finance analysts enter a multiple for each company each quarter. They sometimes revise it, for example after the valuation committee reviews it.
- **Cap tables** (`cap_tables.json`): every preferred series each company has issued, with its issue date.
- **Reported marks** (`q2_marks.csv`): Q2 2026 marks as reported to LPs on 2026-08-14.
