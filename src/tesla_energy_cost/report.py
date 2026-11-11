"""Render a Bill as Markdown, CSV, or JSON."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .billing import Bill, PERIOD_ORDER
from .periods import PERIOD_LABELS
from .util import fmt_cents, fmt_kwh, fmt_money, human_hours

UTC = timezone.utc


def _local(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _period_summary(bill: Bill) -> list[tuple[str, float, float, float]]:
    """Aggregate energy lines by period label for display."""
    out: dict[str, list[float]] = {}
    for line in bill.energy_lines:
        key = line.label
        if line.effective_from and len({l.effective_from for l in bill.energy_lines}) > 1:
            key = f"{line.label} (from {line.effective_from})"
        bucket = out.setdefault(key, [0.0, 0.0, 0.0])
        bucket[0] += line.kwh
        bucket[1] += line.amount
        bucket[2] = line.rate
    return [(k, v[0], v[2], v[1]) for k, v in out.items()]


def render_markdown(bill: Bill, *, meta: dict[str, Any], tz) -> str:
    lines: list[str] = []
    add = lines.append

    title = meta.get("title") or "EV Charging Statement"
    add(f"# {title}")
    add("")
    add(f"**Premises:** {meta.get('premises', '—')}  ")
    add(f"**Utility:** {meta.get('utility', 'Toronto Hydro')} ({meta.get('rate_class', 'Residential Service')})  ")
    add(f"**Price plan:** {_plan_name(bill.plan)}  ")
    add(f"**Meter source:** `{meta.get('source', '—')}`  ")
    add(
        f"**Period:** {_local(bill.start, tz)} → {_local(bill.end, tz)} "
        f"({meta.get('timezone', 'America/Toronto')})  "
    )
    add(f"**Statement date:** {meta.get('statement_date', '')}  ")
    add("")

    add("## Amount due")
    add("")
    add("| | |")
    add("|---|---:|")
    add(f"| **Total due for EV charging** | **{fmt_money(bill.amount_due)}** |")
    add(f"| Energy used | {fmt_kwh(bill.kwh)} kWh |")
    add(f"| Effective all-in rate | {fmt_cents(bill.amount_due / bill.kwh)}/kWh |")
    add("")

    if bill.method == "proportional" and bill.proportional:
        p = bill.proportional
        add(
            f"> **Method:** proportional share — {p.share:.2%} of the household's "
            f"{fmt_money(p.household_bill_total)} bill "
            f"({fmt_kwh(p.household_kwh)} kWh total)."
        )
        add("")
        add(
            f"> Marginal-cost reference (what the car actually added): "
            f"**{fmt_money(bill.total)}**."
        )
        add("")

    add("## Energy by price period")
    add("")
    add("| Price period | kWh | Rate | Amount |")
    add("|---|---:|---:|---:|")
    for label, kwh, rate, amount in _period_summary(bill):
        add(f"| {label} | {fmt_kwh(kwh)} | {fmt_cents(rate)} | {fmt_money(amount)} |")
    add(f"| **Electricity subtotal** | **{fmt_kwh(bill.kwh)}** | | **{fmt_money(bill.electricity)}** |")
    add("")

    add("## Bill calculation")
    add("")
    add("| Line | Basis | Amount |")
    add("|---|---|---:|")
    add(f"| Electricity | commodity rates above | {fmt_money(bill.electricity)} |")
    add(
        f"| Delivery (volumetric) | {fmt_cents(bill.delivery_per_kwh)}/kWh "
        f"(incl. loss factor {bill.loss_factor:.4f}) | {fmt_money(bill.delivery)} |"
    )
    add(
        f"| Regulatory | {fmt_cents(bill.regulatory_per_kwh)}/kWh | "
        f"{fmt_money(bill.regulatory)} |"
    )
    add(f"| **Subtotal** | | **{fmt_money(bill.subtotal)}** |")
    add(f"| HST {bill.hst_rate:.0%} | on subtotal | {fmt_money(bill.hst)} |")
    add(
        f"| Ontario Electricity Rebate {bill.oer_rate:.1%} | credit on subtotal | "
        f"−{fmt_money(bill.oer)} |"
    )
    add(f"| **Total{' due' if bill.method == 'marginal' else ''}** | | **{fmt_money(bill.total)}** |")
    add("")

    add("## Data quality")
    add("")
    add(f"- Intervals billed: **{bill.interval_count:,}**")
    add(f"- Time coverage: **{bill.coverage:.2%}**")
    if bill.coarse_kwh:
        add(f"- Coarse/gap energy: **{fmt_kwh(bill.coarse_kwh)} kWh**")
    if bill.gaps:
        add(f"- Gaps ≥ 5 minutes: **{len(bill.gaps)}**")
        for start, end in bill.gaps[:8]:
            add(f"  - {_local(start, tz)} → {_local(end, tz)} ({human_hours((end - start).total_seconds())})")
        if len(bill.gaps) > 8:
            add(f"  - … and {len(bill.gaps) - 8} more")
    else:
        add("- No gaps ≥ 5 minutes.")
    add("")

    if bill.assumptions:
        add("## Assumptions")
        add("")
        for item in bill.assumptions:
            add(f"- {item}")
        add("")

    add("## Why the fixed charges are excluded")
    add("")
    add(
        "The customer charge, smart-metering charge, and other per-30-day charges "
        "are identical whether or not the car charges, so they are not part of the "
        "car's cost. Only the volumetric charges that grow with kWh are billed here. "
        "This is the fair marginal-cost method; if you prefer to share the whole "
        "bill proportionally, run with `--method proportional`."
    )
    add("")

    add("## Rate sources")
    add("")
    ctx = bill.context
    add(f"- Commodity card effective **{ctx.commodity.effective_from}** — {ctx.commodity.source or 'see config/rates.toml'}")
    add(f"- Delivery card effective **{ctx.delivery.effective_from}** — {ctx.delivery.source or 'see config/rates.toml'}")
    add(f"- Regulatory card effective **{ctx.regulatory.effective_from}** — {ctx.regulatory.source or 'see config/rates.toml'}")
    add(f"- OER **{ctx.rebate.oer:.1%}** effective **{ctx.rebate.effective_from}** — {ctx.rebate.source or 'see config/rates.toml'}")
    add(f"- Loss factor **{ctx.loss_factor}**")
    add("")
    add(
        f"<sub>Generated by tesla-energy-cost {__version__} on "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}. "
        "Verify once against a real Toronto Hydro bill before the first payment.</sub>"
    )
    add("")
    return "\n".join(lines)


def _plan_name(plan: str) -> str:
    return {
        "tou": "Time-of-Use (TOU)",
        "ulo": "Ultra-Low Overnight (ULO)",
        "tiered": "Tiered",
    }.get(plan, plan)


def render_csv(bill: Bill) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["section", "label", "kwh", "rate_per_kwh", "amount"])
    for label, kwh, rate, amount in _period_summary(bill):
        writer.writerow(["energy", label, f"{kwh:.6f}", f"{rate:.6f}", f"{amount:.2f}"])
    writer.writerow(["energy", "Electricity subtotal", f"{bill.kwh:.6f}", "", f"{bill.electricity:.2f}"])
    writer.writerow(["delivery", "Delivery (volumetric)", "", f"{bill.delivery_per_kwh:.6f}", f"{bill.delivery:.2f}"])
    writer.writerow(["regulatory", "Regulatory", "", f"{bill.regulatory_per_kwh:.6f}", f"{bill.regulatory:.2f}"])
    writer.writerow(["subtotal", "Subtotal", "", "", f"{bill.subtotal:.2f}"])
    writer.writerow(["tax", f"HST {bill.hst_rate:.0%}", "", "", f"{bill.hst:.2f}"])
    writer.writerow(["credit", f"OER {bill.oer_rate:.1%}", "", "", f"{-bill.oer:.2f}"])
    writer.writerow(["total", "Total due" if bill.method == "marginal" else "Marginal total", "", "", f"{bill.total:.2f}"])
    if bill.proportional:
        writer.writerow(
            [
                "proportional",
                f"{bill.proportional.share:.6f} of {bill.proportional.household_bill_total:.2f}",
                "",
                "",
                f"{bill.proportional.amount:.2f}",
            ]
        )
    return out.getvalue()


def bill_to_dict(bill: Bill, *, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": meta,
        "period": {"start_utc": bill.start.isoformat(), "end_utc": bill.end.isoformat()},
        "plan": bill.plan,
        "method": bill.method,
        "kwh": round(bill.kwh, 6),
        "energy_lines": [
            {
                "period": line.period,
                "label": line.label,
                "kwh": round(line.kwh, 6),
                "rate_per_kwh": line.rate,
                "amount": line.amount,
                "effective_from": line.effective_from,
            }
            for line in bill.energy_lines
        ],
        "charges": {
            "electricity": bill.electricity,
            "delivery": bill.delivery,
            "regulatory": bill.regulatory,
            "subtotal": bill.subtotal,
            "hst": bill.hst,
            "oer_credit": -bill.oer,
            "total": bill.total,
        },
        "effective_rate_per_kwh": round(bill.effective_rate, 6),
        "delivery_per_kwh": round(bill.delivery_per_kwh, 6),
        "regulatory_per_kwh": round(bill.regulatory_per_kwh, 6),
        "loss_factor": bill.loss_factor,
        "hst_rate": bill.hst_rate,
        "oer_rate": bill.oer_rate,
        "coverage": round(bill.coverage, 6),
        "gaps": [
            {"start_utc": s.isoformat(), "end_utc": e.isoformat()} for s, e in bill.gaps
        ],
        "interval_count": bill.interval_count,
        "coarse_kwh": round(bill.coarse_kwh, 6),
        "amount_due": bill.amount_due,
        "proportional": (
            {
                "share": round(bill.proportional.share, 6),
                "household_kwh": bill.proportional.household_kwh,
                "household_bill_total": bill.proportional.household_bill_total,
                "amount": bill.proportional.amount,
            }
            if bill.proportional
            else None
        ),
        "assumptions": bill.assumptions,
        "rates": {
            "commodity_effective_from": str(bill.context.commodity.effective_from),
            "delivery_effective_from": str(bill.context.delivery.effective_from),
            "regulatory_effective_from": str(bill.context.regulatory.effective_from),
            "rebate_effective_from": str(bill.context.rebate.effective_from),
        },
        "generator": f"tesla-energy-cost {__version__}",
    }


def render_json(bill: Bill, *, meta: dict[str, Any]) -> str:
    return json.dumps(bill_to_dict(bill, meta=meta), indent=2)
