"""Command-line interface for tesla-energy-cost."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .billing import BillingError, PERIOD_ORDER, build_bill, plan_comparison
from .config import Config, ConfigError, load_config
from .ingest import csvio
from .ingest import shelly, tesla
from .ingest.shelly import ShellyError
from .ingest.tesla import TeslaError
from .periods import PERIOD_LABELS, build_segments, coverage_seconds, holidays, parts_by_segment
from .rates import load_rates
from .rateupdate import (
    RateUpdateError,
    apply_update,
    render_report,
    run_update,
)
from .report import render_csv, render_json, render_markdown
from .storage import (
    add_interval,
    add_reading,
    connect,
    derive_intervals_from_readings,
    intervals_between,
    list_readings,
    list_sources,
    parse_stored,
    record_counter_sample,
    record_power_sample,
    merged_ranges,
)
from .util import (
    InputError,
    fmt_cents,
    fmt_kwh,
    fmt_money,
    local_midnight,
    month_label,
    parse_datetime,
    previous_calendar_month,
)


class CliError(RuntimeError):
    """A user-facing command failure."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config(args: argparse.Namespace, *, require_rates: bool = True) -> Config:
    explicit = getattr(args, "root", None)
    root = Path(explicit).expanduser().resolve() if explicit else None
    try:
        return load_config(root)
    except ConfigError:
        if require_rates:
            raise


def _source(args: argparse.Namespace, config: Config, default: str | None = None) -> str:
    return str(getattr(args, "source", None) or default or config.source)


def _month_bounds(month: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    try:
        year, month_number = (int(part) for part in month.split("-", 1))
        start_day = date(year, month_number, 1)
    except (ValueError, TypeError) as exc:
        raise CliError(f"bad --month {month!r}; use YYYY-MM") from exc
    if month_number == 12:
        next_day = date(year + 1, 1, 1)
    else:
        next_day = date(year, month_number + 1, 1)
    return local_midnight(start_day, tz), local_midnight(next_day, tz)


def _resolve_range(
    args: argparse.Namespace, config: Config
) -> tuple[datetime, datetime, str]:
    tz = config.timezone
    month = getattr(args, "month", None)
    from_raw = getattr(args, "date_from", None)
    to_raw = getattr(args, "date_to", None)

    if month and (from_raw or to_raw):
        raise CliError("use either --month or --from/--to, not both")
    if month:
        start, end = _month_bounds(month, tz)
        return start, end, month
    if from_raw or to_raw:
        if not (from_raw and to_raw):
            raise CliError("--from and --to must be used together")
        start = parse_datetime(from_raw, tz)
        end = parse_datetime(to_raw, tz, end_of_day=True)
        if end <= start:
            raise CliError("--to must be after --from")
        return start, end, _slug_range(start, end, tz)
    start, end = previous_calendar_month(tz)
    return start, end, start.astimezone(tz).strftime("%Y-%m")


def _slug_range(start: datetime, end: datetime, tz: ZoneInfo) -> str:
    first = start.astimezone(tz).strftime("%Y%m%d")
    last = (end - timedelta(seconds=1)).astimezone(tz).strftime("%Y%m%d")
    return f"{first}-{last}"


def _meta(
    config: Config,
    source: str,
    start: datetime,
    end: datetime,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    tz = config.timezone
    return {
        "title": title or f"EV Charging Statement — {month_label(start, end, tz)}",
        "premises": config.get("utility.premises", ""),
        "utility": config.get("utility.name", "Toronto Hydro"),
        "rate_class": config.get("utility.rate_class", "Residential Service"),
        "source": source,
        "timezone": str(tz),
        "statement_date": datetime.now(tz).strftime("%Y-%m-%d"),
    }


def _load_intervals(config: Config, source: str, start: datetime, end: datetime):
    conn = connect(config.storage_path)
    intervals = intervals_between(conn, source, start, end)
    if not intervals:
        available = list_sources(conn)
        hint = ", ".join(f"{name} ({count} intervals)" for name, count, *_ in available)
        raise CliError(
            f"source {source!r} has no meter data between "
            f"{start.astimezone(config.timezone):%Y-%m-%d} and "
            f"{end.astimezone(config.timezone):%Y-%m-%d}."
            + (f" Sources in the database: {hint}." if hint else " Import a CSV or poll a meter first.")
        )
    return intervals


def _make_bill(config: Config, args: argparse.Namespace):
    tz = config.timezone
    start, end, slug = _resolve_range(args, config)
    source = _source(args, config)
    schedule = load_rates(config)
    intervals = _load_intervals(config, source, start, end)
    method = getattr(args, "method", None) or config.method
    plan = getattr(args, "plan", None) or config.plan
    assume = getattr(args, "assume_period", None)
    if assume is None:
        assume = config.assume_period
    household_kwh = getattr(args, "household_kwh", None)
    if household_kwh is None:
        household_kwh = config.household_kwh
    household_bill = getattr(args, "household_bill_total", None)
    if household_bill is None:
        household_bill = config.get("billing.household_bill_total")
        household_bill = float(household_bill) if household_bill is not None else None

    bill = build_bill(
        schedule=schedule,
        intervals=intervals,
        start=start,
        end=end,
        plan=plan,
        method=method,
        assume_period=assume,
        household_kwh=household_kwh,
        household_bill_total=household_bill,
    )
    meta = _meta(config, source, start, end, title=getattr(args, "title", None))
    return bill, meta, slug, source


def _render(bill, meta: dict[str, Any], fmt: str, tz: ZoneInfo) -> str:
    if fmt == "md":
        return render_markdown(bill, meta=meta, tz=tz)
    if fmt == "csv":
        return render_csv(bill)
    if fmt == "json":
        return render_json(bill, meta=meta)
    raise CliError(f"unknown output format {fmt!r}")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "source"


def _table(headers: list[str], rows: list[list[str]], *, right: set[int] | None = None) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    right = right or set()

    def line(cells: list[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i])
            for i, cell in enumerate(cells)
        ).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def _local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _num(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

LOCAL_TEMPLATE = """\
# Personal settings for tesla-energy-cost.
# This file is git-ignored. Every value here overrides config/config.toml.

# timezone = "America/Toronto"
# currency = "CAD"

# [billing]
# plan = "tou"              # tou | ulo | tiered
# method = "marginal"       # marginal | proportional
# household_monthly_kwh = 900
# household_bill_total = 0  # only for method = "proportional"

# [meter]
# source = "shelly-plug"
# assume_period_for_coarse_data = "off_peak"

# [meter.shelly]
# host = "192.168.1.50"

# [tesla]
# vehicle_id = "1234567890"

# [utility]
# premises = "123 Example St, Toronto, ON"
# account = "Toronto Hydro account number"
"""


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else Path.cwd().resolve()
    rates = root / "config" / "rates.toml"
    if not rates.is_file():
        raise CliError(
            f"{root} does not look like a tesla-energy-cost checkout "
            f"(missing {rates}). Run this from the repository root, or pass --root."
        )

    for path in (root / "data", root / "statements", root / "config"):
        path.mkdir(parents=True, exist_ok=True)

    config = load_config(root)
    connect(config.storage_path).close()

    local = root / "config" / "local.toml"
    if not local.is_file():
        _write_text(local, LOCAL_TEMPLATE)
        created = "created config/local.toml"
    else:
        created = "kept existing config/local.toml"

    print(f"Initialized {root}")
    print(f"  database:   {config.storage_path}")
    print(f"  statements: {config.statement_dir}")
    print(f"  {created}")
    print()
    print("Next steps:")
    print("  1. Edit config/local.toml (plan, meter source, utility details).")
    print("  2. Import or poll meter data:")
    print("       tec poll shelly --host 192.168.1.50")
    print("       tec import csv usage.csv --source shelly-plug")
    print("  3. Check rates:   tec rates update")
    print("  4. Create a bill: tec monthly --month YYYY-MM")
    print("  5. Read the README for hardware and accuracy guidance.")
    return 0


# ---------------------------------------------------------------------------
# rates
# ---------------------------------------------------------------------------


def _period_label(plan: str, period: str) -> str:
    return PERIOD_LABELS.get(period, period)


def cmd_rates_show(args: argparse.Namespace) -> int:
    config = _config(args)
    schedule = load_rates(config)
    day = parse_datetime(args.date, config.timezone).astimezone(config.timezone).date() if args.date else date.today()
    ctx = schedule.context(day)

    if args.json:
        payload = {
            "as_of": day.isoformat(),
            "currency": schedule.currency,
            "hst": schedule.hst,
            "loss_factor": ctx.loss_factor,
            "delivery_per_kwh": ctx.delivery_per_kwh,
            "regulatory_per_kwh": ctx.regulatory_per_kwh,
            "commodity": {
                "effective_from": ctx.commodity.effective_from.isoformat(),
                "source": ctx.commodity.source,
                "tou": ctx.commodity.tou,
                "ulo": ctx.commodity.ulo,
                "tiered": ctx.commodity.tiered,
            },
            "delivery": {
                "effective_from": ctx.delivery.effective_from.isoformat(),
                "components": ctx.delivery.components,
                "adjustment": ctx.delivery.adjustment,
                "fixed_per_30_days": ctx.delivery.fixed_per_30_days,
            },
            "regulatory": {
                "effective_from": ctx.regulatory.effective_from.isoformat(),
                "components": ctx.regulatory.components,
            },
            "rebate": {
                "effective_from": ctx.rebate.effective_from.isoformat(),
                "oer": ctx.rebate.oer,
            },
            "all_in": {
                plan: {
                    period: ctx.all_in_per_kwh(ctx.commodity.rate(plan, period))
                    for period in PERIOD_ORDER[plan]
                }
                for plan in ("tou", "ulo", "tiered")
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Toronto Hydro rates effective {day.isoformat()}")
    print(f"Source: {schedule.source_path}")
    print()
    print(f"  Commodity card:  {ctx.commodity.effective_from}  {ctx.commodity.source}")
    print(f"  Delivery card:   {ctx.delivery.effective_from}  {ctx.delivery.source}")
    print(f"  Regulatory card: {ctx.regulatory.effective_from}  {ctx.regulatory.source}")
    print(f"  OER:             {ctx.rebate.oer:.1%} from {ctx.rebate.effective_from}")
    print(f"  HST:             {schedule.hst:.1%}")
    print(f"  Loss factor:     {ctx.loss_factor:.4f}")
    print(
        f"  Delivery:        {fmt_cents(ctx.delivery_per_kwh)}/kWh metered "
        f"({fmt_cents(ctx.delivery.volumetric)}/kWh adjusted + "
        f"{fmt_cents(ctx.delivery.adjustment)} adjustment)"
    )
    print(f"  Regulatory:      {fmt_cents(ctx.regulatory_per_kwh)}/kWh metered")
    print()
    print("All-in marginal cost (commodity + delivery + regulatory, less OER, plus HST)")
    print()
    rows = []
    for plan in ("tou", "ulo", "tiered"):
        for period in PERIOD_ORDER[plan]:
            rate = ctx.commodity.rate(plan, period)
            all_in = ctx.all_in_per_kwh(rate)
            rows.append(
                [
                    plan.upper(),
                    _period_label(plan, period),
                    fmt_cents(rate),
                    fmt_cents(all_in),
                ]
            )
    print(_table(["Plan", "Period", "Commodity", "All-in"], rows))
    print()
    print("Fixed monthly charges are listed in `tec status` but are excluded from EV billing")
    print("because they do not change when the car charges.")
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise CliError("confirmation required; re-run with --yes in a non-interactive session")
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def cmd_rates_update(args: argparse.Namespace) -> int:
    config = _config(args)
    report = run_update(
        config,
        offline=Path(args.offline).expanduser() if args.offline else None,
        check_holidays=not args.no_holidays,
    )
    print(render_report(report))

    if args.check and report.has_changes:
        return 2
    if not args.apply:
        if report.has_changes:
            print()
            print("Run `tec rates update --apply --yes` to write config/rates.auto.toml.")
        return 0

    if report.stale:
        raise CliError(
            "refusing to apply: the fetched page is older than the newest stored card"
        )
    if not args.yes and not _confirm("Write the parsed rates to config/rates.auto.toml?"):
        print("Not written.")
        return 1

    apply_update(config, report)
    print(f"Wrote {config.auto_rates_path}")
    print("History in config/rates.toml was not modified.")
    return 0


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def cmd_import_csv(args: argparse.Namespace) -> int:
    config = _config(args)
    source = _source(args, config)
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise CliError(f"CSV not found: {path}")
    conn = connect(config.storage_path)
    result = csvio.import_csv(
        conn,
        path,
        source=source,
        tz=config.timezone,
        fmt=args.format,
        interval_minutes=args.interval_minutes,
        max_gap_minutes=args.max_gap_minutes,
    )
    print(f"Imported {path}")
    print(f"  source:     {source}")
    print(f"  rows:       {result.rows}")
    print(f"  intervals:  {result.intervals}")
    print(f"  energy:     {fmt_kwh(result.kwh)} kWh")
    print(f"  skipped:    {result.skipped}")
    if result.intervals == 0:
        print()
        print("No intervals were created. Check the CSV format with `tec import csv --help`.")
        return 1
    return 0


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


def _record_shelly(config: Config, args: argparse.Namespace) -> int:
    source = _source(args, config)
    host = args.host or config.get("meter.shelly.host")
    if not host:
        raise CliError("no Shelly host; pass --host or set meter.shelly.host in config/local.toml")
    reading = shelly.read(host, timeout=args.timeout)
    conn = connect(config.storage_path)
    interval = None
    if reading.counter_wh is not None:
        interval = record_counter_sample(
            conn,
            source=source,
            ts=datetime.now(config.timezone),
            counter_wh=reading.counter_wh,
            meta={"device": reading.device, "raw": "shelly"},
        )
    elif reading.power_w is not None:
        interval = record_power_sample(
            conn,
            source=source,
            ts=datetime.now(config.timezone),
            power_w=reading.power_w,
            meta={"device": reading.device, "raw": "shelly"},
        )
    else:
        raise CliError("Shelly returned neither a cumulative counter nor power")

    print(f"Shelly: {reading.device} ({host})")
    if reading.counter_wh is not None:
        print(f"  counter: {reading.counter_wh:,.1f} Wh")
    if reading.power_w is not None:
        print(f"  power:   {reading.power_w:,.1f} W")
    if interval is not None:
        print(f"  stored:  {interval.kwh:,.4f} kWh ({interval.quality})")
    else:
        print("  stored:  baseline sample (no interval yet)")
    return 0


def cmd_poll_shelly(args: argparse.Namespace) -> int:
    return _record_shelly(_config(args), args)


def cmd_poll_tesla(args: argparse.Namespace) -> int:
    config = _config(args)
    vehicle_id = args.vehicle_id or config.get("tesla.vehicle_id")
    if not vehicle_id:
        raise CliError("no Tesla vehicle id; pass --vehicle-id or set tesla.vehicle_id")
    reading = tesla.read(
        str(vehicle_id),
        base_url=args.api_base or config.get("tesla.api_base"),
        timeout=args.timeout,
    )
    source = args.source or "tesla"
    power_w = (reading.charger_power_kw or 0.0) * 1000.0
    if reading.charging_state == "Charging" and reading.charger_power_kw is None:
        raise CliError(
            "car says it is charging but charger_power is missing; refusing to invent a value"
        )
    conn = connect(config.storage_path)
    interval = record_power_sample(
        conn,
        source=source,
        ts=reading.timestamp,
        power_w=power_w,
        max_gap_seconds=args.max_gap_seconds,
        meta={"raw": "tesla", "charging_state": reading.charging_state},
    )
    print(f"Tesla vehicle {vehicle_id}: {reading.charging_state or 'unknown state'}")
    print(f"  charger power: {power_w:,.1f} W")
    if reading.battery_level is not None:
        print(f"  battery:       {reading.battery_level}%")
    if interval is not None:
        print(f"  stored:        {interval.kwh:,.4f} kWh over {interval.seconds / 60:.1f} min")
    else:
        print("  stored:        baseline sample (no interval yet)")
    print()
    print("NOTE: Tesla API data is approximate and not billing-grade. Use a physical")
    print("      meter (Shelly Plug / EM) or manual readings for a landlord statement.")
    return 0


# ---------------------------------------------------------------------------
# readings and sessions
# ---------------------------------------------------------------------------


def cmd_reading_add(args: argparse.Namespace) -> int:
    config = _config(args)
    source = _source(args, config)
    ts = parse_datetime(args.at, config.timezone)
    conn = connect(config.storage_path)
    add_reading(conn, source=source, ts=ts, total_kwh=args.kwh, note=args.note or "")
    print(f"Stored reading: {_local(ts, config.timezone)}  {args.kwh:,.3f} kWh  source={source}")
    print("Run `tec reading derive` to turn consecutive readings into intervals.")
    return 0


def cmd_reading_list(args: argparse.Namespace) -> int:
    config = _config(args)
    source = args.source
    conn = connect(config.storage_path)
    readings = list_readings(conn, source)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "source": reading.source,
                        "timestamp": reading.ts.isoformat(),
                        "timestamp_local": _local(reading.ts, config.timezone),
                        "total_kwh": reading.total_kwh,
                        "note": reading.note,
                    }
                    for reading in readings
                ],
                indent=2,
            )
        )
        return 0
    if not readings:
        print("No manual readings stored.")
        return 0
    rows = []
    previous: dict[str, float] = {}
    for reading in readings:
        delta = ""
        if reading.source in previous:
            delta = f"{reading.total_kwh - previous[reading.source]:+,.3f}"
        previous[reading.source] = reading.total_kwh
        rows.append(
            [
                reading.source,
                _local(reading.ts, config.timezone),
                f"{reading.total_kwh:,.3f}",
                delta,
                reading.note,
            ]
        )
    print(_table(["Source", "Local time", "Meter kWh", "Delta", "Note"], rows, right={2, 3}))
    return 0


def cmd_reading_derive(args: argparse.Namespace) -> int:
    config = _config(args)
    source = _source(args, config)
    conn = connect(config.storage_path)
    try:
        created = derive_intervals_from_readings(conn, source)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    total = sum(interval.kwh for interval in created)
    print(f"Derived {len(created)} intervals ({total:,.3f} kWh) for source {source!r}.")
    if created:
        print("Coarse readings are exact in energy but not in time-of-day.")
        print("If charging is confined to one price period, set")
        print("meter.assume_period_for_coarse_data in config/local.toml.")
    return 0


def cmd_session_add(args: argparse.Namespace) -> int:
    config = _config(args)
    source = _source(args, config)
    start = parse_datetime(args.start, config.timezone)
    end = parse_datetime(args.end, config.timezone)
    if end <= start:
        raise CliError("--end must be after --start")
    if args.kwh <= 0:
        raise CliError("--kwh must be positive")
    conn = connect(config.storage_path)
    added = add_interval(
        conn,
        source=source,
        start=start,
        end=end,
        kwh=args.kwh,
        quality="measured",
        meta={"note": args.note or "manual session"},
    )
    if not added:
        raise CliError("an interval with those timestamps already exists")
    print(
        f"Added session: {_local(start, config.timezone)} → {_local(end, config.timezone)}  "
        f"{args.kwh:,.3f} kWh  source={source}"
    )
    return 0


# ---------------------------------------------------------------------------
# bill / monthly
# ---------------------------------------------------------------------------


def cmd_bill(args: argparse.Namespace) -> int:
    config = _config(args)
    bill, meta, _slug, _source_name = _make_bill(config, args)
    text = _render(bill, meta, args.format, config.timezone)
    if args.out:
        path = Path(args.out).expanduser()
        _write_text(path, text)
        print(f"Wrote {path}")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_monthly(args: argparse.Namespace) -> int:
    config = _config(args)

    if args.update_rates:
        try:
            report = run_update(config, check_holidays=not args.no_holidays)
            print(render_report(report))
            if report.has_changes:
                if args.yes:
                    apply_update(config, report)
                    print(f"Applied rates to {config.auto_rates_path}")
                elif args.strict_rates:
                    raise CliError(
                        "rates changed but were not applied; re-run with --yes "
                        "or drop --strict-rates"
                    )
                else:
                    print()
                    print(
                        "Rates changed but were not applied. Re-run with --yes to "
                        "apply them, or run `tec rates update --apply --yes`."
                    )
            print()
        except (RateUpdateError, OSError, ValueError) as exc:
            if args.strict_rates:
                raise
            print(f"warning: rate update skipped: {exc}", file=sys.stderr)
            print("Billing with the currently stored rate cards.", file=sys.stderr)
            print()

    bill, meta, slug, source = _make_bill(config, args)
    tz = config.timezone
    base = f"{slug}-{_safe_name(source)}"
    rendered = {
        "md": _render(bill, meta, "md", tz),
        "csv": _render(bill, meta, "csv", tz),
        "json": _render(bill, meta, "json", tz),
    }

    if args.no_write:
        print(rendered[args.format], end="" if rendered[args.format].endswith("\n") else "\n")
        return 0

    paths = {}
    for fmt, text in rendered.items():
        path = config.statement_dir / f"{base}.{fmt}"
        _write_text(path, text)
        paths[fmt] = path

    print(f"Statement for {month_label(bill.start, bill.end, tz)}")
    print(f"  source:     {source}")
    print(f"  energy:     {fmt_kwh(bill.kwh)} kWh")
    print(f"  coverage:   {bill.coverage:.1%}")
    print(f"  amount due: {fmt_money(bill.amount_due)}")
    print()
    for fmt in ("md", "csv", "json"):
        print(f"  {fmt.upper():<4} {paths[fmt]}")
    if bill.assumptions:
        print()
        print("Assumptions:")
        for item in bill.assumptions:
            print(f"  - {item}")
    return 0


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _tou_split(intervals, start: datetime, end: datetime, tz: ZoneInfo) -> dict[str, float]:
    segments = build_segments(start, end, "tou", tz)
    totals = {period: 0.0 for period in ("off_peak", "mid_peak", "on_peak")}
    for interval in intervals:
        overlap_start = max(interval.start, start)
        overlap_end = min(interval.end, end)
        total_seconds = (interval.end - interval.start).total_seconds()
        if overlap_end <= overlap_start or total_seconds <= 0:
            continue
        kwh = interval.kwh * (overlap_end - overlap_start).total_seconds() / total_seconds
        for segment, part in parts_by_segment(
            overlap_start, overlap_end, kwh, segments
        ):
            totals[segment.period] = totals.get(segment.period, 0.0) + part
    return totals


def cmd_compare(args: argparse.Namespace) -> int:
    config = _config(args)
    start, end, _slug = _resolve_range(args, config)
    tz = config.timezone
    manual = [args.off_peak_kwh, args.mid_peak_kwh, args.on_peak_kwh]
    if any(value is not None for value in manual):
        if any(value is None for value in manual):
            raise CliError("provide all three of --off-peak-kwh, --mid-peak-kwh, --on-peak-kwh")
        split = {
            "off_peak": float(args.off_peak_kwh),
            "mid_peak": float(args.mid_peak_kwh),
            "on_peak": float(args.on_peak_kwh),
        }
    else:
        source = _source(args, config)
        intervals = _load_intervals(config, source, start, end)
        split = _tou_split(intervals, start, end, tz)

    total = sum(split.values())
    if total <= 0:
        raise CliError("no energy to compare")
    schedule = load_rates(config)
    household = args.household_kwh
    if household is None:
        household = config.household_kwh
    rows = plan_comparison(
        schedule,
        off_peak_kwh=split["off_peak"],
        mid_peak_kwh=split["mid_peak"],
        on_peak_kwh=split["on_peak"],
        day=start.astimezone(tz).date(),
        household_kwh=household,
    )
    if args.json:
        print(json.dumps({"kwh": split, "plans": rows}, indent=2))
        return 0

    print(f"Plan comparison for {month_label(start, end, tz)}")
    print(
        f"  Off-peak {fmt_kwh(split['off_peak'])} kWh, "
        f"mid-peak {fmt_kwh(split['mid_peak'])} kWh, "
        f"on-peak {fmt_kwh(split['on_peak'])} kWh"
    )
    print()
    table_rows = [
        [row["plan"], fmt_money(float(row["commodity"])), fmt_money(float(row["total"])), f"{float(row['effective_cents']):.2f}¢"]
        for row in rows
    ]
    print(_table(["Plan", "Commodity", "All-in", "All-in rate"], table_rows, right={1, 2, 3}))
    cheapest = min(rows, key=lambda row: float(row["total"]))
    print()
    print(f"Cheapest at this charging pattern: {cheapest['plan']} ({fmt_money(float(cheapest['total']))}).")
    print("ULO comparison assumes all off-peak kWh can move to 11 p.m.–7 a.m.")
    print("Tiered is exact only when household_monthly_kwh is configured.")
    return 0


# ---------------------------------------------------------------------------
# status and doctor
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    schedule = load_rates(config)
    conn = connect(config.storage_path)
    sources = list_sources(conn)
    readings = list_readings(conn)
    tz = config.timezone

    print(f"Project: {config.root}")
    print(f"Database: {config.storage_path}")
    print()
    if sources:
        rows = []
        for source, count, kwh, first, last in sources:
            first_dt = parse_stored(first) if first else None
            last_dt = parse_stored(last) if last else None
            rows.append(
                [
                    source,
                    str(count),
                    fmt_kwh(kwh),
                    _local(first_dt, tz) if first_dt else "",
                    _local(last_dt, tz) if last_dt else "",
                ]
            )
        print(_table(["Source", "Intervals", "kWh", "First", "Last"], rows, right={1, 2}))
    else:
        print("No interval data yet.")
    print(f"Manual readings: {len(readings)}")

    print()
    print("Rate cards")
    print(f"  commodity:  {schedule.commodity[-1].effective_from}")
    print(f"  delivery:   {schedule.delivery[-1].effective_from}")
    print(f"  regulatory: {schedule.regulatory[-1].effective_from}")
    print(f"  OER:        {schedule.rebate[-1].effective_from} ({schedule.rebate[-1].oer:.1%})")
    print(f"  HST:        {schedule.hst:.1%}")
    if config.auto_rates_path.is_file():
        print(f"  auto overlay: {config.auto_rates_path}")
    for warning in schedule.validate():
        print(f"  ! {warning}")

    statements = sorted(config.statement_dir.glob("*.md")) if config.statement_dir.is_dir() else []
    print()
    print(f"Statements: {len(statements)}")
    if statements:
        print(f"  latest: {statements[-1]}")
    return 0


def _check(
    results: list[tuple[str, str, str]], ok: bool, label: str, detail: str, *, required: bool = True
) -> None:
    status = "ok" if ok else ("FAIL" if required else "warn")
    results.append((status, label, detail))


def cmd_doctor(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []
    tz = ZoneInfo("America/Toronto")
    _check(
        results,
        sys.version_info >= (3, 11),
        "Python version",
        f"{sys.version.split()[0]} (need >= 3.11)",
    )

    try:
        config = _config(args)
        _check(results, True, "Project config", str(config.root))
        tz = config.timezone
    except (ConfigError, CliError) as exc:
        _check(results, False, "Project config", str(exc))
        _print_doctor(results)
        return 1

    try:
        schedule = load_rates(config)
        _check(results, True, "Rate cards", str(schedule.source_path))
    except Exception as exc:  # noqa: BLE001
        _check(results, False, "Rate cards", str(exc))
        _print_doctor(results)
        return 1

    warnings = schedule.validate()
    _check(
        results,
        not warnings,
        "Rate freshness",
        "; ".join(warnings) if warnings else "newest cards are current",
        required=False,
    )
    try:
        _check(
            results,
            config.storage_path.parent.exists() or True,
            "Database path",
            str(config.storage_path),
        )
        conn = connect(config.storage_path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        _check(
            results,
            bool(integrity and integrity[0] == "ok"),
            "Database integrity",
            integrity[0] if integrity else "no result",
        )
        sources = list_sources(conn)
    except sqlite3.Error as exc:
        _check(results, False, "Database", str(exc))
        sources = []

    year = datetime.now(tz).year
    for check_year in (year, year + 1):
        days = holidays(check_year)
        _check(
            results,
            len(days) >= 9,
            f"Holiday calendar {check_year}",
            f"{len(days)} observed dates (includes Civic Holiday)",
            required=False,
        )

    previous_start, previous_end = previous_calendar_month(tz)
    if sources:
        for source, count, kwh, _first, _last in sources:
            conn = connect(config.storage_path)
            intervals = intervals_between(conn, source, previous_start, previous_end)
            ranges = merged_ranges(intervals)
            covered = coverage_seconds(ranges, previous_start, previous_end)
            total_seconds = (previous_end - previous_start).total_seconds()
            share = covered / total_seconds if total_seconds else 0
            _check(
                results,
                share >= 0.95,
                f"Last month coverage ({source})",
                f"{share:.1%} of the previous calendar month",
                required=False,
            )
    else:
        _check(
            results,
            False,
            "Meter data",
            "no intervals stored yet; import a CSV or poll a meter",
            required=False,
        )

    configured_source = config.source
    if sources and configured_source not in {row[0] for row in sources}:
        _check(
            results,
            False,
            "Configured source",
            f"meter.source={configured_source!r} has no data",
            required=False,
        )
    else:
        _check(results, True, "Configured source", configured_source)

    token = bool(os.environ.get("TESLA_ACCESS_TOKEN"))
    _check(
        results,
        token,
        "Tesla token",
        "TESLA_ACCESS_TOKEN is set" if token else "not set (only needed for `tec poll tesla`)",
        required=False,
    )
    if config.auto_rates_path.is_file():
        _check(results, True, "Automatic rate overlay", str(config.auto_rates_path))
    else:
        _check(
            results,
            False,
            "Automatic rate overlay",
            "none yet; run `tec rates update --apply --yes`",
            required=False,
        )

    _check(
        results,
        config.statement_dir.parent.exists(),
        "Statement directory",
        str(config.statement_dir),
        required=False,
    )

    if args.network:
        host = config.get("meter.shelly.host")
        if host:
            try:
                reading = shelly.read(host, timeout=args.timeout)
                detail = f"{reading.device}: {reading.power_w or 0:.1f} W"
                if reading.counter_wh is not None:
                    detail += f", counter {reading.counter_wh:,.0f} Wh"
                _check(results, True, "Shelly reachable", detail, required=False)
            except ShellyError as exc:
                _check(results, False, "Shelly reachable", str(exc), required=False)
        else:
            _check(results, False, "Shelly reachable", "no meter.shelly.host configured", required=False)

    _print_doctor(results)
    return 1 if any(status == "FAIL" for status, _, _ in results) else 0


def _print_doctor(results: list[tuple[str, str, str]]) -> None:
    print("tesla-energy-cost doctor")
    print("=" * 60)
    for status, label, detail in results:
        print(f"[{status:^4}] {label}")
        if detail:
            print(f"         {detail}")
    failures = sum(1 for status, _, _ in results if status == "FAIL")
    warnings = sum(1 for status, _, _ in results if status == "warn")
    print()
    if failures:
        print(f"{failures} required check(s) failed, {warnings} warning(s).")
    else:
        print(f"All required checks passed, {warnings} warning(s).")


# ---------------------------------------------------------------------------
# calibrate (read-only helper)
# ---------------------------------------------------------------------------


def cmd_calibrate(args: argparse.Namespace) -> int:
    config = _config(args)
    schedule = load_rates(config)
    day = (
        parse_datetime(args.effective_from, config.timezone).astimezone(config.timezone).date()
        if args.effective_from
        else date.today()
    )
    ctx = schedule.context(day)
    if args.kwh <= 0:
        raise CliError("--kwh must be positive")
    expected = args.kwh * ctx.delivery.volumetric * ctx.loss_factor
    expected += args.kwh * ctx.delivery.adjustment
    actual = args.delivery
    difference = actual - expected
    per_kwh = difference / args.kwh
    new_adjustment = ctx.delivery.adjustment + per_kwh

    print(f"Calibration for {day.isoformat()} (delivery card {ctx.delivery.effective_from})")
    print(f"  metered kWh:        {args.kwh:,.3f}")
    print(f"  published delivery: {fmt_money(expected)}")
    print(f"  bill delivery:      {fmt_money(actual)}")
    print(f"  difference:         {fmt_money(difference)} ({fmt_cents(per_kwh)}/kWh)")
    if args.regulatory is not None:
        expected_reg = args.kwh * ctx.regulatory.volumetric * ctx.loss_factor
        print(f"  published regulatory: {fmt_money(expected_reg)}")
        print(f"  bill regulatory:      {fmt_money(args.regulatory)}")
        print(f"  regulatory difference: {fmt_money(args.regulatory - expected_reg)}")
    print()
    print("If you have verified the bill line, add this to config/rates.local.toml:")
    print()
    print("[[delivery_override]]")
    print(f"effective_from = {ctx.delivery.effective_from}")
    print(f"delivery_adjustment = {new_adjustment:.8f}")
    print()
    print("Do not use a calibration to hide a parsing error; check the tariff first.")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_range_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--month", help="calendar month, YYYY-MM (default: previous month)")
    parser.add_argument("--from", dest="date_from", help="start date/time (inclusive)")
    parser.add_argument("--to", dest="date_to", help="end date/time (inclusive for a bare date)")


def _add_billing_arguments(parser: argparse.ArgumentParser) -> None:
    _add_range_arguments(parser)
    parser.add_argument("--source", help="meter source label (default: meter.source)")
    parser.add_argument("--plan", choices=["tou", "ulo", "tiered"], help="price plan")
    parser.add_argument(
        "--method",
        choices=["marginal", "proportional"],
        help="marginal cost, or share of the whole household bill",
    )
    parser.add_argument(
        "--assume-period",
        help="assign coarse reading energy to one period (off_peak, ultra_low_overnight, ...)",
    )
    parser.add_argument("--household-kwh", type=float, help="household total kWh for tiered/proportional")
    parser.add_argument(
        "--household-bill-total", type=float, help="household bill total for proportional method"
    )
    parser.add_argument("--title", help="statement title")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tec",
        description=(
            "Meter-accurate EV electricity billing for Ontario renters. "
            "Toronto Hydro rates, TOU/ULO periods, OER and HST included."
        ),
    )
    parser.add_argument("--root", help="project root (default: nearest parent with config/rates.toml)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create data directories, local config, and database")
    init.set_defaults(func=cmd_init)

    # rates -----------------------------------------------------------------
    rates = sub.add_parser("rates", help="inspect or update Toronto Hydro rates")
    rates_sub = rates.add_subparsers(dest="rates_command", required=True)

    rates_show = rates_sub.add_parser("show", help="show effective rates and all-in marginal prices")
    rates_show.add_argument("--date", help="show the card in force on this date")
    rates_show.add_argument("--json", action="store_true", help="machine-readable output")
    rates_show.set_defaults(func=cmd_rates_show)

    rates_update = rates_sub.add_parser(
        "update", help="fetch Toronto Hydro/OEB pages and compare with stored rates"
    )
    rates_update.add_argument("--apply", action="store_true", help="write config/rates.auto.toml")
    rates_update.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    rates_update.add_argument("--check", action="store_true", help="exit 2 when changes are available")
    rates_update.add_argument("--offline", help="parse a saved HTML file instead of the network")
    rates_update.add_argument("--no-holidays", action="store_true", help="skip the OEB holiday check")
    rates_update.set_defaults(func=cmd_rates_update)

    # import ----------------------------------------------------------------
    imp = sub.add_parser("import", help="import meter data from CSV")
    imp_sub = imp.add_subparsers(dest="import_command", required=True)
    imp_csv = imp_sub.add_parser("csv", help="import an intervals/energy/power/counter/daily CSV")
    imp_csv.add_argument("path", help="CSV file")
    imp_csv.add_argument("--source", help="meter source label")
    imp_csv.add_argument(
        "--format",
        choices=["auto", "intervals", "energy", "power", "counter", "daily"],
        default="auto",
    )
    imp_csv.add_argument(
        "--interval-minutes",
        type=float,
        help="for energy CSVs: length of the first interval (timestamp is the end)",
    )
    imp_csv.add_argument(
        "--max-gap-minutes",
        type=float,
        default=60,
        help="power CSVs: do not integrate across gaps longer than this (default 60)",
    )
    imp_csv.set_defaults(func=cmd_import_csv)

    # poll ------------------------------------------------------------------
    poll = sub.add_parser("poll", help="read a local meter once")
    poll_sub = poll.add_subparsers(dest="poll_command", required=True)

    poll_shelly = poll_sub.add_parser("shelly", help="read a Shelly Gen2/Gen3 device over HTTP")
    poll_shelly.add_argument("--host", help="IP/hostname (default: meter.shelly.host)")
    poll_shelly.add_argument("--source", help="meter source label")
    poll_shelly.add_argument("--timeout", type=float, default=5.0)
    poll_shelly.set_defaults(func=cmd_poll_shelly)

    poll_tesla = poll_sub.add_parser("tesla", help="read Tesla Fleet API charge_state (approximate)")
    poll_tesla.add_argument("--vehicle-id", help="Fleet API vehicle id (default: tesla.vehicle_id)")
    poll_tesla.add_argument("--api-base", help="regional Fleet API host")
    poll_tesla.add_argument("--source", help="meter source label (default: tesla)")
    poll_tesla.add_argument("--timeout", type=float, default=20.0)
    poll_tesla.add_argument("--max-gap-seconds", type=float, default=600)
    poll_tesla.set_defaults(func=cmd_poll_tesla)

    # reading ---------------------------------------------------------------
    reading = sub.add_parser("reading", help="manage manual cumulative meter readings")
    reading_sub = reading.add_subparsers(dest="reading_command", required=True)

    reading_add = reading_sub.add_parser("add", help="store a cumulative meter reading")
    reading_add.add_argument("--kwh", type=float, required=True, help="meter total in kWh")
    reading_add.add_argument("--at", required=True, help="timestamp of the reading")
    reading_add.add_argument("--source", help="meter source label")
    reading_add.add_argument("--note", help="optional note")
    reading_add.set_defaults(func=cmd_reading_add)

    reading_list = reading_sub.add_parser("list", help="list stored readings")
    reading_list.add_argument("--source", help="filter by source")
    reading_list.add_argument("--json", action="store_true")
    reading_list.set_defaults(func=cmd_reading_list)

    reading_derive = reading_sub.add_parser("derive", help="turn consecutive readings into intervals")
    reading_derive.add_argument("--source", help="meter source label")
    reading_derive.set_defaults(func=cmd_reading_derive)

    # session ---------------------------------------------------------------
    session = sub.add_parser("session", help="manage manual charging sessions")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_add = session_sub.add_parser("add", help="add one measured charging interval")
    session_add.add_argument("--start", required=True)
    session_add.add_argument("--end", required=True)
    session_add.add_argument("--kwh", type=float, required=True)
    session_add.add_argument("--source", help="meter source label")
    session_add.add_argument("--note")
    session_add.set_defaults(func=cmd_session_add)

    # bill ------------------------------------------------------------------
    bill = sub.add_parser("bill", help="calculate a bill for one period")
    _add_billing_arguments(bill)
    bill.add_argument("--format", choices=["md", "csv", "json"], default="md")
    bill.add_argument("--out", help="write to a file instead of stdout")
    bill.set_defaults(func=cmd_bill)

    monthly = sub.add_parser(
        "monthly", help="update rates (optional) and write a statement for one month"
    )
    _add_billing_arguments(monthly)
    monthly.add_argument(
        "--update-rates",
        action="store_true",
        help="check Toronto Hydro/OEB first; with --yes, apply changes",
    )
    monthly.add_argument("--yes", action="store_true", help="apply rate changes non-interactively")
    monthly.add_argument("--no-holidays", action="store_true", help="skip the OEB holiday check")
    monthly.add_argument(
        "--strict-rates",
        action="store_true",
        help="fail instead of billing with stored rates if the update fails",
    )
    monthly.add_argument("--format", choices=["md", "csv", "json"], default="md")
    monthly.add_argument("--no-write", action="store_true", help="print instead of writing files")
    monthly.set_defaults(func=cmd_monthly)

    # compare ---------------------------------------------------------------
    compare = sub.add_parser("compare", help="compare TOU/ULO/tiered for a charging pattern")
    _add_range_arguments(compare)
    compare.add_argument("--source", help="meter source label")
    compare.add_argument("--off-peak-kwh", type=float)
    compare.add_argument("--mid-peak-kwh", type=float)
    compare.add_argument("--on-peak-kwh", type=float)
    compare.add_argument("--household-kwh", type=float)
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=cmd_compare)

    # status / doctor -------------------------------------------------------
    status = sub.add_parser("status", help="show data, rate cards, and statements")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="run environment and data-quality checks")
    doctor.add_argument("--network", action="store_true", help="also probe the configured Shelly")
    doctor.add_argument("--timeout", type=float, default=5.0)
    doctor.set_defaults(func=cmd_doctor)

    calibrate = sub.add_parser(
        "calibrate", help="compare a real bill with the published delivery charge"
    )
    calibrate.add_argument("--kwh", type=float, required=True, help="metered kWh on the bill")
    calibrate.add_argument("--delivery", type=float, required=True, help="delivery charge on the bill")
    calibrate.add_argument("--regulatory", type=float, help="regulatory charge on the bill")
    calibrate.add_argument("--effective-from", help="date used to select the delivery card")
    calibrate.set_defaults(func=cmd_calibrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (CliError, ConfigError, BillingError, InputError, RateUpdateError, ShellyError, TeslaError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
