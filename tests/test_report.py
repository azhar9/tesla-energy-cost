from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tesla_energy_cost.billing import build_bill
from tesla_energy_cost.rates import load_rates
from tesla_energy_cost.report import bill_to_dict, render_csv, render_json, render_markdown
from tesla_energy_cost.storage import Interval
from tesla_energy_cost.util import local_midnight

TORONTO = ZoneInfo("America/Toronto")


def _bill(project):
    schedule = load_rates(project)
    start = local_midnight(datetime(2026, 11, 2).date(), TORONTO)
    end = start + timedelta(days=1)
    interval = Interval(
        "test",
        datetime(2026, 11, 2, 12, 0, tzinfo=TORONTO),
        datetime(2026, 11, 2, 13, 0, tzinfo=TORONTO),
        10.0,
    )
    return build_bill(
        schedule=schedule,
        intervals=[interval],
        start=start,
        end=end,
        plan="tou",
    )


def test_markdown_contains_landlord_facing_sections(project):
    bill = _bill(project)
    text = render_markdown(bill, meta={"premises": "Toronto"}, tz=TORONTO)
    assert "# EV Charging Statement" in text
    assert "## Amount due" in text
    assert "Electricity" in text
    assert "Delivery (volumetric)" in text
    assert "Ontario Electricity Rebate" in text
    assert "Why the fixed charges are excluded" in text
    assert "Toronto" in text


def test_csv_has_total_rows(project):
    bill = _bill(project)
    rows = list(csv.reader(io.StringIO(render_csv(bill))))
    assert rows[0][0] == "section"
    sections = {row[0] for row in rows[1:]}
    assert {"energy", "delivery", "regulatory", "subtotal", "tax", "credit", "total"} <= sections


def test_json_round_trips(project):
    bill = _bill(project)
    payload = json.loads(render_json(bill, meta={"premises": "Toronto"}))
    assert payload["plan"] == "tou"
    assert payload["charges"]["total"] == bill.total
    assert payload["kwh"] == 10.0
    assert payload["generator"].startswith("tesla-energy-cost")


def test_bill_to_dict_marks_oer_as_negative(project):
    bill = _bill(project)
    payload = bill_to_dict(bill, meta={})
    assert payload["charges"]["oer_credit"] < 0
    assert payload["charges"]["total"] == bill.total
