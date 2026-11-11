# Demo data

`demo_intervals.csv` is synthetic, hourly Level 1 charging for November 1–9,
2026, with a tiny standby value so every hour has a measured interval. It
includes overnight off-peak charging plus one mid-peak and one on-peak session,
so the statement exercises TOU splitting.

`demo_statement.md` was generated with:

```bash
tec import csv examples/demo_intervals.csv --source demo
tec bill --from 2026-11-01 --to 2026-11-09 --source demo \
  --format md --out examples/demo_statement.md
```

The demo is not a real bill and is not a template for your own usage. Generate
your own statement from your meter data.
