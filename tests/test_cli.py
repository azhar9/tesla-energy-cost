from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tesla_energy_cost.cli import main
from tesla_energy_cost.storage import add_interval, connect

TORONTO = ZoneInfo("America/Toronto")


def _seed(project):
    conn = connect(project.storage_path)
    start = datetime(2026, 11, 2, 12, 0, tzinfo=TORONTO)
    add_interval(
        conn,
        source="test",
        start=start,
        end=start + timedelta(hours=1),
        kwh=10.0,
    )
    return conn


def test_rates_show(project, capsys):
    code = main(["--root", str(project.root), "rates", "show"])
    assert code == 0
    out = capsys.readouterr().out
    assert "All-in marginal cost" in out
    assert "11.79" in out


def test_rates_update_offline_writes_overlay(project, fixtures, capsys):
    code = main(
        [
            "--root",
            str(project.root),
            "rates",
            "update",
            "--offline",
            str(fixtures / "toronto_hydro_rates.html"),
            "--no-holidays",
            "--apply",
            "--yes",
        ]
    )
    assert code == 0
    assert project.auto_rates_path.is_file()
    out = capsys.readouterr().out
    assert "Wrote" in out


def test_bill_json(project, capsys):
    _seed(project)
    code = main(
        [
            "--root",
            str(project.root),
            "bill",
            "--month",
            "2026-11",
            "--source",
            "test",
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kwh"] == 10.0
    assert payload["amount_due"] > 0


def test_monthly_writes_three_formats(project, capsys):
    _seed(project)
    code = main(
        [
            "--root",
            str(project.root),
            "monthly",
            "--month",
            "2026-11",
            "--source",
            "test",
        ]
    )
    assert code == 0
    base = project.statement_dir / "2026-11-test"
    assert (base.with_suffix(".md")).is_file()
    assert (base.with_suffix(".csv")).is_file()
    assert (base.with_suffix(".json")).is_file()
    assert "amount due" in capsys.readouterr().out


def test_status_lists_source(project, capsys):
    _seed(project)
    code = main(["--root", str(project.root), "status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "test" in out
    assert "10.00" in out


def test_doctor_runs(project, capsys):
    _seed(project)
    code = main(["--root", str(project.root), "doctor"])
    # Missing Tesla token / overlay are warnings, not failures.
    assert code == 0
    out = capsys.readouterr().out
    assert "All required checks passed" in out


def test_missing_source_is_reported(project, capsys):
    code = main(
        [
            "--root",
            str(project.root),
            "bill",
            "--month",
            "2026-11",
            "--source",
            "does-not-exist",
        ]
    )
    assert code == 1
    assert "no meter data" in capsys.readouterr().err.lower()
