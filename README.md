# tesla-energy-cost

[![tests](https://github.com/azhar9/tesla-energy-cost/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/azhar9/tesla-energy-cost/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Meter-accurate electricity billing for a Tesla charged at a rented home in
Toronto, Ontario — built so a landlord can be paid **exactly** what the car
added to the Toronto Hydro bill, month after month.

I rent a house in Toronto and charge a 2021 Tesla Model 3 Standard Range Plus
from an ordinary outlet (Level 1) or, when available, a 240 V Level 2 circuit.
My landlord asked for the exact cost of the charging, not a guess. A
generic "kWh × price" calculator is wrong here: a Toronto Hydro bill also
contains volumetric delivery charges, regulatory charges, a loss factor, the
Ontario Electricity Rebate (OER), and HST — and the OER/HST interaction is easy
to get backwards. Rates also change (commodity every November 1, delivery and
regulatory every January 1, OER whenever the province changes it), so a
hand-edited spreadsheet rots quietly.

This repository is the answer to that problem:

- **Measure at the wall.** A physical meter records the energy that actually
  entered the car, including charging losses. The Tesla API is supported only
  as an approximate trend, never as the landlord's meter.
- **Price like the utility.** Each interval is assigned to the correct
  TOU/ULO/tier period, including weekends, statutory holidays, and DST.
- **Include every volumetric line.** Commodity + delivery + regulatory, adjusted
  by the published loss factor, less OER, plus HST.
- **Keep an audit trail.** Every statement is reproducible from the stored
  intervals and effective-dated rate cards. Rate updates never rewrite history.
- **Update rates automatically.** The updater fetches Toronto Hydro and OEB
  pages, cross-checks them, snapshots the HTML, and writes an overlay file.

It is deliberately small: Python 3.11+, standard library only, SQLite for
storage, Markdown/CSV/JSON statements.

> **Scope:** this calculates the *utility-bill cost* of the charging. It is not
> legal, tax, or tenancy advice. See [Landlord agreement](#landlord-agreement)
> before you rely on it.

---

## Quick start

```bash
git clone https://github.com/azhar9/tesla-energy-cost.git
cd tesla-energy-cost
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"

tec init
$EDITOR config/local.toml     # plan, meter source, premises
tec doctor                    # environment + data checks
tec rates show                # verify the rate cards
```

A minimal `config/local.toml` for a Level 1 smart plug looks like this:

```toml
[billing]
plan = "tou"                  # or "ulo" if the account is on Ultra-Low Overnight
method = "marginal"

[meter]
source = "shelly-plug"

[meter.shelly]
host = "192.168.1.50"

[utility]
premises = "123 Example St, Toronto, ON"
```

The committed `config/config.toml` documents every setting. `config/local.toml`
is git-ignored, so your address and account details never enter the repository.

### First month

```bash
# Option A: a Shelly on the charging circuit
tec poll shelly --host 192.168.1.50

# Option B: import a CSV exported by the meter
tec import csv examples/demo_intervals.csv --source demo

# Option C: one cumulative reading at the start and end of the month
tec reading add --at "2026-11-01 00:00" --kwh 1200.0
tec reading add --at "2026-12-01 00:00" --kwh 1480.0
tec reading derive

# Bill the previous calendar month and write statements/
tec monthly --month 2026-11
```

`tec monthly` writes `statements/YYYY-MM-<source>.md`, `.csv`, and `.json`.
The Markdown file is the one to send to the landlord; the CSV/JSON are for
verification and record-keeping.

A generated example is in
[`examples/demo_statement.md`](examples/demo_statement.md), and its input is
[`examples/demo_intervals.csv`](examples/demo_intervals.csv). The demo uses
November 1–9, 2026 rates: 80.86 kWh, **$9.76** all-in, 12.07¢/kWh effective.
`tec compare` can differ from that total by a cent or two: the statement rounds
each bill line to cents, while the comparison multiplies an unrounded all-in
rate by the period kWh.

---

## Hardware: what is actually billing-grade?

| Setup | Recommended hardware | Accuracy | Notes |
|---|---|---|---|
| Level 1, 120 V, 12 A | **Shelly Plus Plug US** (Gen2, model `SNPL-00116US`; UL-listed, 15 A) | ±1% typical | Reads the whole EVSE. Use the local RPC API; no cloud account. Must be rated for the outlet and protected from weather. **Do not buy the original "Shelly Plug US"** — it is a Gen1 device and is not supported. |
| Level 2, 240 V, 32 A | **Shelly EM / Pro EM-50** with a 50 A CT, installed by a licensed electrician | ±1–2% | Do **not** use a consumer smart plug for 32 A continuous. A CT around the EV circuit in the panel is the practical option. |
| Any | Manual cumulative meter readings | Exact energy, unknown timing | Cheapest. If all charging is overnight, set `meter.assume_period_for_coarse_data = "off_peak"` (or `"ultra_low_overnight"`). |
| Any | Tesla Fleet API | Approximate | `charger_power` polling misses sessions, reports at the car/charger boundary, and can wake the car. **Not billing-grade.** |
| Any | Tesla Wall Connector / car's own session history | Approximate | Useful as a cross-check, not as the meter of record. |

The important rule: the meter must sit on the **EV circuit** (or the EV outlet),
so it measures wall energy delivered to the charging equipment. A CT in the
panel must not include the house's other loads. Charging losses mean the wall
energy is a few percent higher than the battery energy shown in the car; the
wall meter already includes that, which is what the utility charges for.

For Level 1, the Tesla Mobile Connector draws about 12 A at 120 V (~1.44 kW).
For Level 2 with the 14-50 adapter it draws up to 32 A at 240 V (~7.7 kW). The
demo data assumes Level 1.

The Shelly adapter supports Gen2/Gen3 `switch`, `em`, and `em1` components.
It prefers the cumulative energy counter, so a missed poll does not lose energy.
Power-only readings are integrated trapezoidally and are only accepted across
short gaps.

Gen1 devices are **not** supported: they expose `/status` and `/emeter/0`
instead of `/rpc/Shelly.GetStatus`. That includes the original **Shelly Plug
US**, which is still sold alongside the Plus model. Check the model number
before buying — `SNPL-00116US` is the Plus Plug US; the original has no "Plus"
in its name. If you already own a Gen1 plug, export its readings to CSV and use
`tec import csv` instead.

TP-Link Kasa plugs are not a shortcut either. Current KP125M firmware advertises
**TPAP**, TP-Link's newer local protocol, which `python-kasa` does not implement
(at the time of writing, 0.12.0). Home Assistant's Kasa integration hits the
same limit. A Kasa plug can still be used through a CSV export or a manual
monthly reading, but it cannot be polled by `tec` today.

---

## How the bill is calculated

The default `method = "marginal"` charges only the costs that change when the
car charges:

```text
commodity   = Σ (kWh in each TOU/ULO/tier period × that period's rate)
delivery    = metered kWh × (Σ volumetric delivery components × loss factor
                             + delivery_adjustment)
regulatory  = metered kWh × Σ regulatory components × loss factor
subtotal    = commodity + delivery + regulatory
HST         = 13% × subtotal
OER credit  = OER% × subtotal
total       = subtotal + HST − OER credit
```

Each component is rounded to cents (half-up) and then summed, matching utility
bill presentation.

### Current cards (verified 2026-11-10)

| Component | Effective | Value |
|---|---|---|
| TOU commodity | 2026-11-01 | off-peak 9.1¢, mid-peak 14.6¢, on-peak 18.9¢ /kWh |
| ULO commodity | 2026-11-01 | ultra-low overnight 3.7¢, weekend off-peak 9.1¢, mid-peak 14.6¢, on-peak 37.7¢ /kWh |
| Tiered commodity | 2026-11-01 | Tier 1 11.2¢, Tier 2 13.2¢ /kWh; 600 kWh summer / 1,000 kWh winter |
| Delivery (volumetric) | 2026-01-01 | deferral/variance 0.112¢, capacity-based recovery 0.048¢, transmission 2.241¢ /kWh adjusted |
| Regulatory | 2026-01-01 | wholesale market service 0.53¢ /kWh adjusted |
| Loss factor | 2026-01-01 | 1.0295 |
| OER | 2026-11-01 | 15.7% credit on the pre-HST subtotal |
| HST | — | 13% on the pre-rebate subtotal |

Sources: [Toronto Hydro residential rates](https://www.torontohydro.com/for-home/rates),
the [OEB historical rate table](https://www.oeb.ca/consumer-information-and-protection/electricity-rates/historical-electricity-rates),
and Toronto Hydro's 2026 Tariff of Rates and Charges (EB-2025-0006). The values
are stored in `config/rates.toml`; `tec rates show` prints the effective set and
`tec rates update` checks it against the live pages.

### Why the fixed charges are excluded

The customer charge, smart-metering charge, pole-attachment riders, and other
per-30-day amounts are the same whether the car is plugged in or not. Charging
them to the EV would overcharge the landlord. The statement says this
explicitly, which makes the method easy to defend.

If the landlord prefers "share the whole bill", use:

```bash
tec monthly --method proportional \
  --household-kwh 850 --household-bill-total 165.00
```

That charges `EV kWh / household kWh × total bill`. It is simpler to verify but
slightly overpays, because the EV pays a share of fixed charges too.

### OER and HST — the subtle part

The OER is a credit calculated on the **pre-HST subtotal**, and HST is charged
on the **full pre-rebate subtotal**. The net multiplier is therefore:

```text
1 + HST − OER        (not (1 − OER) × (1 + HST))
```

At the November 2026 values that is `1 + 0.13 − 0.157 = 0.973`. Getting this
wrong changes the total by about 2% — small per month, but not "exact".

### Loss factor

Delivery and regulatory charges are billed on metered kWh adjusted by Toronto
Hydro's Total Loss Factor. The current card uses **1.0295**, confirmed against
Toronto Hydro's own sample bill (700 kWh metered → 720.65 kWh adjusted) and the
2026 tariff (EB-2025-0006). `delivery_adjustment` is applied per metered kWh and
is deliberately **not** multiplied by the loss factor.

### TOU, ULO, holidays, and DST

- **TOU:** off-peak 7 p.m.–7 a.m. weekdays plus all weekend/holiday hours;
  mid-peak 11 a.m.–5 p.m.; on-peak 7–11 a.m. and 5–7 p.m.
- **ULO:** ultra-low overnight 11 p.m.–7 a.m. every day; weekend/holiday
  off-peak 7 a.m.–11 p.m.; weekday mid-peak 7 a.m.–4 p.m. and 9–11 p.m.;
  on-peak 4–9 p.m.
- **Holidays** follow the OEB schedule, including the Civic Holiday and the
  weekend-observation rule (e.g. Boxing Day 2026 → Monday, December 28).
- **DST:** periods are built in local wall-clock time and converted to UTC, so
  the spring-forward day is 23 hours and the fall-back day is 25 hours.
- **Rate changes mid-month:** each interval is priced with the card in force on
  its local date, so a November 1 commodity change or January 1 delivery change
  is handled without special cases.

### Tiered plans

Tiered pricing depends on the **household's** total monthly kWh, not just the
car's. Supply it with `--household-kwh` (or `billing.household_monthly_kwh`).
Without it, the tool prices all EV kWh at Tier 1 and says so in the statement.

---

## Automatic rate updates

```bash
tec rates update                 # fetch, parse, cross-check, print a diff
tec rates update --check         # exit 2 if anything changed (for monitoring)
tec rates update --apply --yes   # write config/rates.auto.toml
```

The updater:

1. Fetches the Toronto Hydro residential rates page, the OEB historical rate
   table, and the OEB holiday schedule.
2. Parses commodity (TOU/ULO/tiered), delivery components, regulatory
   components, OER, HST, and tier thresholds.
3. Cross-checks commodity and thresholds against the OEB table. A mismatch is
   a warning, not a silent override; Toronto Hydro remains the source of truth
   for delivery, regulatory, and OER.
4. Checks the computed holiday calendar against the OEB schedule for the year.
5. Snapshots every fetched page under `data/rate-snapshots/<UTC timestamp>/`
   with a SHA-256 in `meta.json`.
6. Writes an **overlay** to `config/rates.auto.toml`. Same-effective-date cards
   replace earlier ones; all other history is preserved. `config/rates.toml`
   is never rewritten.

The overlay is merged in this order, so later files win on the same effective
date:

```text
config/rates.toml  →  config/rates.local.toml  →  config/rates.auto.toml
```

If a fetch is stale (older than the newest stored card), `--apply` refuses to
write. If a required value cannot be parsed, the command fails loudly instead
of guessing. That is intentional: a silent zero or a swapped peak rate would
produce a wrong landlord statement.

The one number not scraped is the **loss factor**: it lives in the tariff PDF,
not the HTML rates page, and is stable between tariffs. The committed card is
confirmed against Toronto Hydro's sample bill; `tec doctor` warns when it has
not been reviewed for over 400 days. Check the tariff if that warning appears.

Run the update weekly, or at least on the 1st of each month before billing. The
monthly automation below does it automatically. Rate snapshots and the overlay
are git-ignored by default; keep the overlay if you want an audit copy.

---

## Automation

### macOS launchd

```bash
mkdir -p ~/Library/LaunchAgents scripts
sed "s|__PROJECT_ROOT__|$PWD|g; s|__PYTHON__|$PWD/.venv/bin/python|g" \
  scripts/com.tec.monthly.plist > ~/Library/LaunchAgents/com.tec.monthly.plist
launchctl load ~/Library/LaunchAgents/com.tec.monthly.plist
```

It runs at 08:00 on the 1st of every month:

```text
tec monthly --update-rates --yes
```

Logs go to `data/monthly.log`. The plist is a template; inspect it before
loading.

### cron

```cron
0 8 1 * * cd /path/to/tesla-energy-cost && .venv/bin/tec monthly --update-rates --yes >> data/monthly.log 2>&1
0 9 * * 1 cd /path/to/tesla-energy-cost && .venv/bin/tec rates update --check >> data/rates.log 2>&1
```

The second line exits with status 2 when rates changed; cron will usually email
that output. It is a reminder to review and apply, not an automatic write.

### Polling

Poll at least every 5 minutes if you rely on power integration. A cumulative
counter is more forgiving: a 15-minute poll still recovers all energy, though
the time-of-day split becomes coarser beyond 15 minutes:

```cron
*/5 * * * * cd /path/to/tesla-energy-cost && .venv/bin/tec poll shelly >> data/poll.log 2>&1
```

For a Shelly Plug, the local API is enough; no cloud token is needed. For the
optional Tesla adapter:

```bash
export TESLA_ACCESS_TOKEN="..."
tec poll tesla --vehicle-id 1234567890 --source tesla
```

Keep the token in the environment or your secret manager, never in the repo.

---

## CSV import

`tec import csv FILE --source NAME` auto-detects these schemas (header names
are normalised, so `Energy (kWh)` works):

| Format | Required columns | Meaning |
|---|---|---|
| `intervals` | `start`, `end`, `kwh` | One row per measured interval. |
| `energy` | `timestamp`, `kwh` | Timestamp is the **end** of the interval. The first row is skipped unless `--interval-minutes` is given. |
| `power` | `timestamp`, `power_w` | Samples integrated trapezoidally; gaps over `--max-gap-minutes` (default 60) are not integrated. |
| `counter` | `timestamp`, `total_wh` | Cumulative meter counter; deltas are exact. Counter resets are ignored rather than invented. |
| `daily` | `date`, `kwh` | One coarse value per day. |

Naive timestamps are interpreted in `America/Toronto`; ISO timestamps with an
offset or `Z` are respected. Bare dates mean local midnight.

Manual cumulative readings are the simplest option if the meter has a display:

```bash
tec reading add --at "2026-11-01 00:00" --kwh 1200.0
tec reading add --at "2026-12-01 00:00" --kwh 1480.0
tec reading derive
```

Energy is exact; timing is not. If all charging is in one period, set
`meter.assume_period_for_coarse_data` to that period and the statement will say
so. Otherwise the coarse energy is spread evenly across the day and the
statement flags that assumption.

---

## Command reference

| Command | What it does |
|---|---|
| `tec init` | Create `data/`, `statements/`, `config/local.toml`, and the SQLite database. |
| `tec rates show [--date D] [--json]` | Show effective cards and all-in marginal cents/kWh. |
| `tec rates update [--apply --yes] [--check] [--offline FILE]` | Fetch, verify, diff, and optionally write the rate overlay. |
| `tec import csv FILE --source S` | Import intervals/energy/power/counter/daily CSV. |
| `tec poll shelly --host H` | Read a Shelly Gen2/Gen3 meter once. |
| `tec poll tesla --vehicle-id ID` | Read Tesla `charge_state` once (approximate). |
| `tec reading add/list/derive` | Manage manual cumulative readings. |
| `tec session add --start ... --end ... --kwh ...` | Add one measured charging session. |
| `tec bill --month YYYY-MM [--format md\|csv\|json]` | Calculate a bill for any period. |
| `tec monthly [--month YYYY-MM]` | Bill the previous month, write all three formats. |
| `tec compare [--month YYYY-MM]` | Compare TOU/ULO/tiered for the measured charging pattern. |
| `tec status` | Show sources, intervals, rate cards, and statements. |
| `tec doctor [--network]` | Check config, rates, holidays, DB, last-month coverage, and optionally the Shelly. |
| `tec calibrate --kwh N --delivery $N` | Compare a real bill line with the tariff and print a calibration snippet. |

Useful flags:

- `--root PATH` — point at a project checkout from anywhere.
- `--plan tou|ulo|tiered`, `--method marginal|proportional`.
- `--assume-period` — assign coarse readings to one period.
- `--household-kwh`, `--household-bill-total` — tiered/proportional inputs.
- `tec monthly --update-rates --yes` — update rates, then bill.
- `tec monthly --strict-rates` — fail instead of billing with stored rates if
  the update fails.

Exit codes: `0` success, `1` user/data error, `2` `rates update --check` found
changes.

---

## Accuracy and caveats

This tool aims to reproduce the **volumetric** part of a Toronto Hydro
Residential Service bill. It is not a utility meter and not a legal invoice.

- **Meter accuracy.** Shelly and CT meters are typically ±1–2%. The utility's
  revenue meter is the final authority. For a landlord statement, round to the
  cent and show the meter source; do not claim more precision than the hardware
  supports.
- **Measurement boundary.** Measure the EV circuit, not the whole house. A CT
  must exclude other loads. A plug meter includes the EVSE's standby draw,
  which is tiny but real.
- **Polling gaps.** A cumulative counter recovers energy across gaps; power
  integration does not. `tec status`/`tec doctor` and the statement show
  coverage and gaps. If coverage is low, fix the poller before trusting the
  month.
- **Tesla API.** `charger_power` is sampled, not integrated by Tesla; sessions
  can be missed; the car may sleep. It is a convenience for a trend, not the
  billing source.
- **Plan mismatch.** The account may be TOU, ULO, or Tiered. Confirm it on the
  landlord's bill and set `billing.plan`. The wrong plan can change the result
  substantially.
- **Rate class.** This is calibrated for Toronto Hydro **Residential Service**.
  Suite-metered service, a retailer contract, or a different utility will have
  different delivery/regulatory lines. Do not reuse the cards blindly.
- **Fixed charges.** They are excluded by design. If the landlord wants them
  shared, use the proportional method and say so in writing.
- **OER eligibility.** The OER is applied at the published percentage. If the
  account is not eligible, or the bill shows a different effective rebate, the
  statement will not match. Check one real bill.
- **HST treatment of the reimbursement.** This calculates the HST embedded in
  the utility bill. Whether the landlord must charge/remit HST on a
  reimbursement is a tax question; ask an accountant.
- **Rate changes and rounding.** Effective-dated cards handle changes, but a
  utility bill may round per line differently. A few cents of difference is
  expected; tens of dollars means a wrong card or plan.
- **Not legal or tax advice.** See below.

The repo includes a `tec calibrate` helper: give it the metered kWh and the
delivery charge from a real bill; it prints the implied `delivery_adjustment`
for `config/rates.local.toml`. Use it only after checking that the tariff and
rate class match — never to hide a parsing error.

---

## Landlord agreement

Before the first payment, agree in writing on:

1. **The meter.** Which device, where it is installed, who can read it, and
   what happens if it fails.
2. **The method.** Marginal cost (recommended) or proportional share. State
   that fixed monthly charges are excluded from the marginal method.
3. **The rate source.** Toronto Hydro's published Residential Service rates,
   with the effective date shown on each statement.
4. **Timing.** Billing period (calendar month), statement date, and payment
   date.
5. **Verification.** The landlord may see the meter reading/CSV and the rate
   calculation. Offer the Markdown statement plus the CSV.
6. **Rate changes.** The statement uses the rates in force on each day; no
   retroactive repricing.
7. **Disputes.** A real Toronto Hydro bill line wins over this tool. Use
   `tec calibrate` or the proportional method to reconcile.

In Ontario, a landlord may recover the **actual cost** of electricity used by
the tenant; they should not profit from it. The Residential Tenancies Act and
LTB rules are specific, and this is not legal advice. If the amount is material,
or the lease is unclear, get advice from a tenant/landlord lawyer or the LTB.
Keep the statements and meter evidence; they are your audit trail.

---

## Repository layout

```text
config/
  config.toml          committed defaults (plan, meter, utility)
  rates.toml           effective-dated Toronto Hydro cards, never rewritten
  local.toml           your overrides (git-ignored)
  rates.local.toml     manual rate overrides (git-ignored)
  rates.auto.toml      generated by `tec rates update` (git-ignored)
src/tesla_energy_cost/
  cli.py               all `tec` commands
  billing.py           interval pricing, OER/HST, proportional method
  periods.py           TOU/ULO periods, holidays, DST-safe segmenting
  rates.py             effective-dated cards and marginal maths
  rateupdate.py        fetch/parse/cross-check/snapshot/overlay
  storage.py           SQLite samples, intervals, readings
  report.py            Markdown/CSV/JSON statements
  ingest/              CSV, Shelly, Tesla adapters
tests/                 pytest suite and saved HTML fixtures
examples/              demo intervals and a generated statement
scripts/               launchd and cron templates
statements/            generated statements (git-ignored)
data/                  SQLite DB, snapshots, logs (git-ignored)
```

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The tests cover holidays (including the OEB fixture), DST segmenting, TOU/ULO
splits, OER/HST ordering, tiered allocation, CSV formats, counter resets,
statement rendering, the HTML rate parsers, and CLI smoke tests. The live
parsers are exercised against saved fixtures in `tests/fixtures/`, so a change
to the Toronto Hydro page is caught by `tec rates update` rather than silently
changing a bill.

## Privacy

Everything is local: SQLite in `data/`, statements in `statements/`, snapshots
in `data/rate-snapshots/`. No usage data is sent anywhere. The only outbound
requests are the rate pages and, if you explicitly use it, the Tesla Fleet API.
The Tesla token is read from `TESLA_ACCESS_TOKEN`, not stored in config.

## Contributing

Issues and pull requests are welcome. Before opening a PR:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The rate parsers are tested against saved HTML fixtures in `tests/fixtures/`.
If Toronto Hydro changes its page, update the fixture and parser together; do
not make the parser guess.

## License

MIT. See [LICENSE](LICENSE).
