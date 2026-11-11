"""Automatically fetch, verify, and apply Toronto Hydro / OEB rate updates.

Design rules
------------
* The published pages are the source of truth, but we never *guess*: if a
  required value cannot be parsed, the update fails loudly.
* Updates are written to ``config/rates.auto.toml`` as an overlay. History in
  ``config/rates.toml`` is never rewritten, so old statements stay reproducible.
* Every fetch is snapshotted (HTML + SHA-256 + timestamp) under
  ``data/rate-snapshots/`` for audit.
* Two independent sources are cross-checked: Toronto Hydro (delivery,
  regulatory, OER) and the OEB (commodity, tier thresholds, holidays).

Run ``tec rates update`` to preview, ``tec rates update --apply --yes`` to
write. ``--check`` exits 2 when something changed (useful in cron).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from .config import Config
from .periods import holidays as computed_holidays
from .rates import (
    CommodityCard,
    DeliveryCard,
    RateSchedule,
    RebateCard,
    RegulatoryCard,
    load_rates,
)

RATES_URL = "https://www.torontohydro.com/for-home/rates"
OER_URL = "https://www.torontohydro.com/for-home/ontario-electricity-rebate"
OEB_HISTORICAL_URL = (
    "https://www.oeb.ca/consumer-information-and-protection/electricity-rates/"
    "historical-electricity-rates"
)
OEB_HOLIDAYS_URL = (
    "https://www.oeb.ca/consumer-information-and-protection/electricity-rates/"
    "holiday-schedule-time-use-and-ultra-low"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36 tesla-energy-cost/0.1"
)


class RateUpdateError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Fetch + snapshot
# ---------------------------------------------------------------------------


@dataclass
class FetchedPage:
    url: str
    text: str
    fetched_at: datetime
    sha256: str


def fetch_page(url: str, *, timeout: float = 30.0) -> FetchedPage:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-CA,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise RateUpdateError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RateUpdateError(f"network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RateUpdateError(f"timeout fetching {url}") from exc

    text = raw.decode(charset, errors="replace")
    return FetchedPage(
        url=url,
        text=text,
        fetched_at=datetime.now(timezone.utc),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def snapshot(pages: Iterable[FetchedPage], root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = root / "data" / "rate-snapshots" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    meta = []
    for page in pages:
        name = re.sub(r"[^a-zA-Z0-9]+", "_", page.url).strip("_")[-80:] + ".html"
        (folder / name).write_text(page.text, encoding="utf-8")
        meta.append(
            {
                "url": page.url,
                "file": name,
                "sha256": page.sha256,
                "fetched_at": page.fetched_at.isoformat(),
            }
        )
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return folder


# ---------------------------------------------------------------------------
# HTML outline
# ---------------------------------------------------------------------------


@dataclass
class Node:
    kind: str  # heading | para | table
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)
    caption: str = ""


class OutlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[Node] = []
        self._capture: tuple[str, int, list[str]] | None = None
        self._in_table = False
        self._table: list[list[str]] = []
        self._table_caption = ""
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._in_table = True
            self._table = []
            self._table_caption = ""
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "h4" and self._in_table:
            # Toronto Hydro puts each delivery table's title inside a <caption>.
            self._capture = ("caption", 4, [])
        elif tag in ("h1", "h2", "h3", "h4") and not self._in_table:
            self._capture = ("heading", int(tag[1]), [])
        elif tag in ("p", "li") and not self._in_table:
            self._capture = ("para", 0, [])
        elif tag == "br":
            self._append(" ")

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._in_table:
            if self._table:
                self.nodes.append(
                    Node("table", rows=self._table, caption=self._table_caption)
                )
            self._in_table = False
            self._table = []
            self._table_caption = ""
        elif tag in ("h1", "h2", "h3", "h4", "p", "li") and self._capture:
            kind, level, parts = self._capture
            text = _clean(" ".join(parts))
            if kind == "caption":
                if text:
                    self._table_caption = text
            elif text:
                self.nodes.append(Node(kind, text=text, level=level))
            self._capture = None

    def _append(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        elif self._capture is not None:
            self._capture[2].append(data)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_outline(html: str) -> list[Node]:
    parser = OutlineParser()
    parser.feed(html)
    parser.close()
    return parser.nodes


@dataclass
class Annotated:
    node: Node
    ctx: dict[int, str]


def annotate(nodes: list[Node]) -> list[Annotated]:
    ctx = {1: "", 2: "", 3: "", 4: ""}
    out: list[Annotated] = []
    for node in nodes:
        if node.kind == "heading":
            ctx[node.level] = node.text
            for level in range(node.level + 1, 5):
                ctx[level] = ""
        out.append(Annotated(node, dict(ctx)))
    return out


def find_table(
    annotated: list[Annotated],
    *,
    h2: str | None = None,
    h3: str | None = None,
    h4: str | None = None,
) -> list[list[str]] | None:
    for item in annotated:
        if item.node.kind != "table":
            continue
        if h2 and h2.lower() not in item.ctx[2].lower():
            continue
        if h3 and h3.lower() not in item.ctx[3].lower():
            continue
        if h4 and h4.lower() not in (item.ctx[4] + " " + item.node.caption).lower():
            continue
        return item.node.rows
    return None


def find_para(
    annotated: list[Annotated],
    pattern: str,
    *,
    h2: str | None = None,
    h3: str | None = None,
) -> str | None:
    rx = re.compile(pattern, re.I)
    for item in annotated:
        if item.node.kind != "para":
            continue
        if h2 and h2.lower() not in item.ctx[2].lower():
            continue
        if h3 and h3.lower() not in item.ctx[3].lower():
            continue
        if rx.search(item.node.text):
            return item.node.text
    return None


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def parse_long_date(text: str) -> date | None:
    cleaned = _clean(text)
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
    cleaned = cleaned.replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(\d{4})",
        cleaned,
        re.I,
    )
    if match:
        month = datetime.strptime(match.group(1).title(), "%B").month
        return date(int(match.group(3)), month, int(match.group(2)))
    return None


def parse_value(text: str) -> tuple[float, str]:
    """Return (value, unit) where unit is 'kwh', 'fixed', or 'percent'."""
    raw = _clean(text).replace("\u2212", "-").replace("\u2013", "-")
    sign = -1.0 if raw.startswith("-") or raw.startswith("(") else 1.0
    raw = re.sub(r"^[-\s(]+", "", raw)

    match = re.search(r"([\d,]+(?:\.\d+)?)\s*\u00a2\s*per\s*kwh", raw, re.I)
    if match:
        return round(sign * float(match.group(1).replace(",", "")) / 100.0, 8), "kwh"

    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*per\s*kwh", raw, re.I)
    if match:
        return round(sign * float(match.group(1).replace(",", "")), 8), "kwh"

    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*per\s*30\s*days", raw, re.I)
    if match:
        return round(sign * float(match.group(1).replace(",", "")), 6), "fixed"

    match = re.search(r"([\d.]+)\s*%", raw)
    if match:
        return round(float(match.group(1)) / 100.0, 8), "percent"

    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", raw)
    if match:
        return round(sign * float(match.group(1).replace(",", "")), 8), "dollars"

    raise RateUpdateError(f"cannot parse a rate from {text!r}")


def _row_value(rows: list[list[str]], label_contains: str, column: int = -1) -> str | None:
    needle = label_contains.lower()
    for row in rows[1:]:
        if row and needle in row[0].lower():
            return row[column]
    return None


def _month_key(label: str) -> str:
    normalized = label.lower().strip()
    mapping = {
        "off-peak": "off_peak",
        "mid-peak": "mid_peak",
        "on-peak": "on_peak",
        "ultra-low overnight": "ultra_low_overnight",
        "weekend off-peak": "weekend_off_peak",
    }
    return mapping.get(normalized, normalized.replace("-", "_").replace(" ", "_"))


def _rider_key(label: str) -> str:
    text = label.lower().replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\s+", " ", text)
    if text == "customer":
        return "service_charge"
    if "smart metering" in text:
        return "smart_metering_entity"
    if "wireline pole" in text:
        return "wireline_pole_attachment"
    if "wireless pole" in text:
        return "wireless_pole_attachment"
    if "capacity based recovery" in text:
        return "capacity_based_recovery"
    if "deferral/variance" in text:
        return "deferral_variance"
    if "global adjustment" in text:
        return "__skip_global_adjustment__"
    if "useful life" in text and "2025-2026" in text:
        return "useful_life_2025"
    if "useful life" in text and "2025-2028" in text:
        return "useful_life_assets_2025"
    if "useful life" in text and "2026-2028" in text:
        return "useful_life_assets_2026"
    if text.startswith("transmission"):
        return "transmission"
    if "standard supply service" in text:
        return "standard_supply_admin"
    if "wholesale market service" in text:
        return "wholesale_market_service"
    return ""


# ---------------------------------------------------------------------------
# Toronto Hydro parser
# ---------------------------------------------------------------------------


@dataclass
class ParsedRates:
    commodity: CommodityCard | None = None
    delivery: DeliveryCard | None = None
    regulatory: RegulatoryCard | None = None
    rebate: RebateCard | None = None
    hst: float | None = None
    oer_previous: float | None = None
    tier_thresholds: tuple[float, float] | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_toronto_hydro(html: str) -> ParsedRates:
    nodes = parse_outline(html)
    annotated = annotate(nodes)
    result = ParsedRates()

    # --- effective date for commodity ------------------------------------
    commodity_date = None
    for item in annotated:
        if item.node.kind == "heading" and re.search(r"rates\s+(as of|effective)", item.node.text, re.I):
            commodity_date = parse_long_date(
                re.sub(r"^rates\s+(as of|effective)\s+", "", item.node.text, flags=re.I)
            )
            break
    if commodity_date is None:
        raise RateUpdateError("could not find 'Rates as of <date>' on the Toronto Hydro page")

    # --- TOU --------------------------------------------------------------
    tou_rows = find_table(annotated, h2="electricity rates", h3="time-of-use")
    if not tou_rows:
        raise RateUpdateError("could not find the TOU rate table")
    tou = {}
    for row in tou_rows[1:]:
        if len(row) < 2:
            continue
        key = _month_key(row[0])
        if key in {"off_peak", "mid_peak", "on_peak"}:
            value, unit = parse_value(row[-1])
            if unit != "kwh":
                raise RateUpdateError(f"TOU rate {row[0]} is not per kWh: {row[-1]!r}")
            tou[key] = value
    if len(tou) != 3:
        raise RateUpdateError(f"incomplete TOU table: {tou}")

    # --- ULO --------------------------------------------------------------
    ulo_rows = find_table(annotated, h2="electricity rates", h3="ultra-low overnight")
    if not ulo_rows:
        raise RateUpdateError("could not find the ULO rate table")
    ulo = {}
    for row in ulo_rows[1:]:
        key = _month_key(row[0])
        if key in {"ultra_low_overnight", "weekend_off_peak", "mid_peak", "on_peak"}:
            value, unit = parse_value(row[-1])
            if unit != "kwh":
                raise RateUpdateError(f"ULO rate {row[0]} is not per kWh")
            ulo[key] = value
    if len(ulo) != 4:
        raise RateUpdateError(f"incomplete ULO table: {ulo}")

    # --- Tiered -----------------------------------------------------------
    tier_rows = find_table(annotated, h2="electricity rates", h3="tiered")
    if not tier_rows:
        raise RateUpdateError("could not find the tiered rate table")
    tiered = {}
    thresholds = []
    for row in tier_rows[1:]:
        key = "tier1" if row[0].lower().startswith("tier 1") else (
            "tier2" if row[0].lower().startswith("tier 2") else ""
        )
        if not key:
            continue
        value, unit = parse_value(row[-1])
        if unit != "kwh":
            raise RateUpdateError(f"tiered rate {row[0]} is not per kWh")
        tiered[key] = value
        match = re.search(r"up to\s+([\d,]+)\s*kwh", " ".join(row), re.I)
        if match:
            thresholds.append(float(match.group(1).replace(",", "")))
    if len(tiered) != 2:
        raise RateUpdateError(f"incomplete tiered table: {tiered}")

    # The distributor page only shows the current season's threshold. The OEB
    # historical table has both; keep this as a fallback only.
    result.tier_thresholds = (
        (thresholds[0], thresholds[-1]) if len(thresholds) >= 2 else None
    )

    result.notes.append(
        "Tier thresholds on the distributor page cover only the current season; "
        "the OEB historical table supplies both."
    )

    # --- Delivery ---------------------------------------------------------
    delivery_date_text = find_para(
        annotated, r"delivery charges.*effective", h2="delivery charges"
    )
    delivery_date = None
    if delivery_date_text:
        match = re.search(r"effective\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", delivery_date_text)
        if match:
            delivery_date = parse_long_date(match.group(1))
    if delivery_date is None:
        raise RateUpdateError("could not find the Delivery Charges effective date")

    dist_rows = find_table(
        annotated, h3="residential service", h4="distribution charges"
    )
    vol_rows = find_table(annotated, h3="residential service", h4="delivery charges and credits")
    tx_rows = find_table(annotated, h3="residential service", h4="transmission charge")
    if not dist_rows or not vol_rows or not tx_rows:
        raise RateUpdateError("could not find all Residential Service delivery tables")

    components: dict[str, float] = {}
    fixed: dict[str, float] = {}
    for rows in (dist_rows, vol_rows, tx_rows):
        for row in rows[1:]:
            if len(row) < 2:
                continue
            key = _rider_key(row[0])
            if not key:
                result.warnings.append(f"unmapped delivery line ignored: {row[0]}")
                continue
            if key == "__skip_global_adjustment__":
                result.notes.append(
                    "Global Adjustment rider ignored: it applies only to non-RPP "
                    "(retailer contract) customers."
                )
                continue
            value, unit = parse_value(row[-1])
            if unit == "fixed":
                fixed[key] = value
            elif unit == "kwh":
                components[key] = value
            else:
                result.warnings.append(f"unexpected unit for {row[0]}: {row[-1]!r}")

    if "service_charge" not in fixed:
        raise RateUpdateError("delivery table has no Customer (service) charge")
    if "transmission" not in components:
        raise RateUpdateError("delivery table has no Transmission charge")

    # --- Regulatory -------------------------------------------------------
    reg_date_text = find_para(
        annotated, r"regulatory charges.*effective", h2="regulatory charges"
    )
    reg_date = None
    if reg_date_text:
        match = re.search(r"effective\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", reg_date_text)
        if match:
            reg_date = parse_long_date(match.group(1))
    if reg_date is None:
        reg_date = delivery_date

    reg_rows = find_table(annotated, h2="regulatory charges")
    if not reg_rows:
        raise RateUpdateError("could not find the Regulatory Charges table")
    reg_components: dict[str, float] = {}
    for row in reg_rows[1:]:
        if len(row) < 2:
            continue
        key = _rider_key(row[0])
        value, unit = parse_value(row[-1])
        if key == "wholesale_market_service" and unit == "kwh":
            reg_components[key] = value
        elif key == "standard_supply_admin" and unit == "fixed":
            fixed[key] = value
        else:
            result.warnings.append(f"unmapped regulatory line ignored: {row[0]}")
    if "wholesale_market_service" not in reg_components:
        raise RateUpdateError("regulatory table has no Wholesale Market Service rate")

    # --- OER + HST --------------------------------------------------------
    oer_text = find_para(
        annotated, r"\boer\b.*%", h3="ontario electricity rebate"
    )
    if not oer_text:
        oer_text = find_para(annotated, r"change to the oer", h2="additional charges")
    oer = None
    oer_date = None
    previous = None
    if oer_text:
        percentages = re.findall(r"([\d.]+)\s*%", oer_text)
        if percentages:
            previous = float(percentages[0]) / 100.0 if len(percentages) > 1 else None
            oer = float(percentages[-1]) / 100.0
        match = re.search(r"effective\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", oer_text)
        if match:
            oer_date = parse_long_date(match.group(1))
    if oer is None:
        raise RateUpdateError("could not find the Ontario Electricity Rebate percentage")
    if oer_date is None:
        oer_date = commodity_date
        result.warnings.append("OER effective date not found; using the commodity date")

    hst_text = find_para(annotated, r"hst.*%", h3="harmonized sales tax")
    hst = None
    if hst_text:
        match = re.search(r"([\d.]+)\s*%", hst_text)
        if match:
            hst = float(match.group(1)) / 100.0

    result.commodity = CommodityCard(
        effective_from=commodity_date,
        tou=tou,
        ulo=ulo,
        tiered=tiered,
        tier_threshold_summer_kwh=(
            result.tier_thresholds[0] if result.tier_thresholds else 600.0
        ),
        tier_threshold_winter_kwh=(
            result.tier_thresholds[1] if result.tier_thresholds else 1000.0
        ),
        source=RATES_URL,
    )
    result.delivery = DeliveryCard(
        effective_from=delivery_date,
        components=components,
        adjustment=0.0,
        fixed_per_30_days=fixed,
        source=RATES_URL,
    )
    result.regulatory = RegulatoryCard(
        effective_from=reg_date,
        components=reg_components,
        source=RATES_URL,
    )
    result.rebate = RebateCard(effective_from=oer_date, oer=oer, source=OER_URL)
    result.hst = hst
    result.oer_previous = previous
    return result


# ---------------------------------------------------------------------------
# OEB validation
# ---------------------------------------------------------------------------


def _table_rows(html: str) -> list[list[list[str]]]:
    return [n.rows for n in parse_outline(html) if n.kind == "table"]


def _parse_effective_date(text: str) -> date | None:
    return parse_long_date(text)


@dataclass
class OebRates:
    tou: dict[date, dict[str, float]] = field(default_factory=dict)
    ulo: dict[date, dict[str, float]] = field(default_factory=dict)
    tiered: dict[date, dict[str, float]] = field(default_factory=dict)
    thresholds: tuple[float, float] | None = None


def parse_oeb_historical(html: str) -> OebRates:
    out = OebRates()
    for rows in _table_rows(html):
        if not rows:
            continue
        header = " | ".join(rows[0]).lower()
        if "off-peak price" in header and "ultra-low" not in header:
            for row in rows[1:]:
                if len(row) < 4:
                    continue
                day = _parse_effective_date(row[0])
                if not day:
                    continue
                out.tou[day] = {
                    "off_peak": round(float(row[1]) / 100.0, 8),
                    "mid_peak": round(float(row[2]) / 100.0, 8),
                    "on_peak": round(float(row[3]) / 100.0, 8),
                }
        elif "ultra-low overnight" in header:
            for row in rows[1:]:
                if len(row) < 5:
                    continue
                day = _parse_effective_date(row[0])
                if not day:
                    continue
                out.ulo[day] = {
                    "ultra_low_overnight": round(float(row[1]) / 100.0, 8),
                    "weekend_off_peak": round(float(row[2]) / 100.0, 8),
                    "mid_peak": round(float(row[3]) / 100.0, 8),
                    "on_peak": round(float(row[4]) / 100.0, 8),
                }
        elif "lower tier price" in header:
            for row in rows[1:]:
                if len(row) < 4:
                    continue
                day = _parse_effective_date(row[0])
                if not day:
                    continue
                out.tiered[day] = {
                    "tier1": round(float(row[1]) / 100.0, 8),
                    "tier2": round(float(row[3]) / 100.0, 8),
                }
                threshold_text = row[2]
                summer = re.search(r"([\d,]+)\s*\(summer\)", threshold_text, re.I)
                winter = re.search(r"([\d,]+)\s*\(winter\)", threshold_text, re.I)
                if summer and winter:
                    out.thresholds = (
                        float(summer.group(1).replace(",", "")),
                        float(winter.group(1).replace(",", "")),
                    )
    return out


def cross_check(
    parsed: ParsedRates, oeb: OebRates, warnings: list[str]
) -> None:
    if parsed.commodity is None:
        return
    day = parsed.commodity.effective_from
    for name, table, parsed_values in (
        ("TOU", oeb.tou, parsed.commodity.tou),
        ("ULO", oeb.ulo, parsed.commodity.ulo),
        ("Tiered", oeb.tiered, parsed.commodity.tiered),
    ):
        official = table.get(day)
        if not official:
            warnings.append(
                f"OEB historical table has no {name} row for {day}; "
                "could not cross-check commodity rates."
            )
            continue
        for key, value in official.items():
            if key in parsed_values and abs(parsed_values[key] - value) > 0.0001:
                warnings.append(
                    f"{name} {key} mismatch: Toronto Hydro "
                    f"{parsed_values[key]*100:.2f}\u00a2 vs OEB {value*100:.2f}\u00a2. "
                    "Trusting Toronto Hydro; check the page."
                )
    if oeb.thresholds:
        parsed.tier_thresholds = oeb.thresholds
        parsed.commodity = CommodityCard(
            effective_from=parsed.commodity.effective_from,
            tou=parsed.commodity.tou,
            ulo=parsed.commodity.ulo,
            tiered=parsed.commodity.tiered,
            tier_threshold_summer_kwh=oeb.thresholds[0],
            tier_threshold_winter_kwh=oeb.thresholds[1],
            source=parsed.commodity.source,
        )


def parse_oeb_holidays(html: str) -> dict[int, set[date]]:
    nodes = parse_outline(html)
    annotated = annotate(nodes)
    year = None
    for item in annotated:
        if item.node.kind == "heading" and re.search(r"\d{4}.*holiday", item.node.text, re.I):
            match = re.search(r"(\d{4})", item.node.text)
            if match:
                year = int(match.group(1))
                break
    if year is None:
        # Fall back to the page's "2026 Time-of-Use..." heading or today's year.
        for item in annotated:
            match = re.search(r"(\d{4})\s+Time-of-Use", item.node.text)
            if match:
                year = int(match.group(1))
                break
    if year is None:
        year = date.today().year

    for rows in _table_rows(html):
        if not rows or "holiday" not in " ".join(rows[0]).lower():
            continue
        found: set[date] = set()
        for row in rows[1:]:
            if not row:
                continue
            text = re.sub(r"^[A-Za-z]+,\s*", "", row[0])
            parsed = parse_long_date(f"{text}, {year}")
            if parsed:
                found.add(parsed)
        if found:
            return {year: found}
    return {}


# ---------------------------------------------------------------------------
# Diff + write
# ---------------------------------------------------------------------------


@dataclass
class Change:
    section: str
    label: str
    old: float | None
    new: float
    unit: str

    @property
    def changed(self) -> bool:
        if self.old is None:
            return True
        return abs(self.old - self.new) > 0.0000005

    def render(self) -> str:
        if self.unit == "kwh":
            old = f"{self.old*100:.2f}\u00a2" if self.old is not None else "—"
            new = f"{self.new*100:.2f}\u00a2"
            delta = f"({(self.new-self.old)*100:+.2f}\u00a2)" if self.old is not None else ""
            return f"  {self.label:<34} {old:>9} → {new:>9}  {delta}"
        if self.unit == "fixed":
            old = f"${self.old:.2f}" if self.old is not None else "—"
            new = f"${self.new:.2f}"
            delta = f"({self.new-self.old:+.2f})" if self.old is not None else ""
            return f"  {self.label:<34} {old:>9} → {new:>9}  {delta}"
        old = f"{self.old:.1%}" if self.old is not None else "—"
        new = f"{self.new:.1%}"
        delta = f"({(self.new-self.old)*100:+.2f} pp)" if self.old is not None else ""
        return f"  {self.label:<34} {old:>9} → {new:>9}  {delta}"


def _existing_card(cards: list, day: date):
    for card in cards:
        if card.effective_from == day:
            return card
    return None


def build_changes(schedule: RateSchedule, parsed: ParsedRates) -> list[Change]:
    changes: list[Change] = []

    if parsed.commodity:
        old = _existing_card(schedule.commodity, parsed.commodity.effective_from)
        pairs = [
            ("TOU", "off_peak", parsed.commodity.tou),
            ("TOU", "mid_peak", parsed.commodity.tou),
            ("TOU", "on_peak", parsed.commodity.tou),
            ("ULO", "ultra_low_overnight", parsed.commodity.ulo),
            ("ULO", "weekend_off_peak", parsed.commodity.ulo),
            ("ULO", "mid_peak", parsed.commodity.ulo),
            ("ULO", "on_peak", parsed.commodity.ulo),
            ("Tiered", "tier1", parsed.commodity.tiered),
            ("Tiered", "tier2", parsed.commodity.tiered),
        ]
        for section, key, values in pairs:
            if key not in values:
                continue
            old_value = None
            if old:
                table = {"TOU": old.tou, "ULO": old.ulo, "Tiered": old.tiered}[section]
                old_value = table.get(key)
            changes.append(Change(section, key, old_value, values[key], "kwh"))

    if parsed.delivery:
        old = _existing_card(schedule.delivery, parsed.delivery.effective_from)
        for key, value in parsed.delivery.components.items():
            old_value = old.components.get(key) if old else None
            changes.append(Change("Delivery", key, old_value, value, "kwh"))
        for key, value in parsed.delivery.fixed_per_30_days.items():
            old_value = old.fixed_per_30_days.get(key) if old else None
            changes.append(Change("Delivery (fixed)", key, old_value, value, "fixed"))

    if parsed.regulatory:
        old = _existing_card(schedule.regulatory, parsed.regulatory.effective_from)
        for key, value in parsed.regulatory.components.items():
            old_value = old.components.get(key) if old else None
            changes.append(Change("Regulatory", key, old_value, value, "kwh"))

    if parsed.rebate:
        old = _existing_card(schedule.rebate, parsed.rebate.effective_from)
        changes.append(
            Change("Rebate", "oer", old.oer if old else None, parsed.rebate.oer, "percent")
        )

    return changes


def _fmt_number(value: float) -> str:
    if abs(value - round(value)) < 1e-12:
        return str(int(round(value)))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _fmt_float(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _fmt_number(float(value))
    raise TypeError(value)


def _inline_table(values: dict[str, float]) -> str:
    return "{ " + ", ".join(f"{k} = {_fmt_float(v)}" for k, v in values.items()) + " }"


def write_auto_rates(
    path: Path,
    *,
    commodity: CommodityCard | None,
    delivery: DeliveryCard | None,
    regulatory: RegulatoryCard | None,
    rebate: RebateCard | None,
    hst: float | None,
    fetched: list[FetchedPage],
) -> None:
    # Merge with any existing overlay, replacing same-effective-date cards.
    existing = _read_existing_auto(path)
    sections = {
        "commodity": existing.get("commodity", []),
        "delivery": existing.get("delivery", []),
        "regulatory": existing.get("regulatory", []),
        "rebate": existing.get("rebate", []),
    }

    def upsert(name: str, card, serializer) -> None:
        if card is None:
            return
        kept = [c for c in sections[name] if c["effective_from"] != card.effective_from.isoformat()]
        kept.append(serializer(card))
        sections[name] = sorted(kept, key=lambda c: c["effective_from"])

    if commodity:
        upsert(
            "commodity",
            commodity,
            lambda c: {
                "effective_from": c.effective_from.isoformat(),
                "source": c.source,
                "tou": c.tou,
                "ulo": c.ulo,
                "tiered": c.tiered,
                "summer": c.tier_threshold_summer_kwh,
                "winter": c.tier_threshold_winter_kwh,
            },
        )
    if delivery:
        upsert(
            "delivery",
            delivery,
            lambda c: {
                "effective_from": c.effective_from.isoformat(),
                "source": c.source,
                "components": c.components,
                "adjustment": c.adjustment,
                "fixed": c.fixed_per_30_days,
            },
        )
    if regulatory:
        upsert(
            "regulatory",
            regulatory,
            lambda c: {
                "effective_from": c.effective_from.isoformat(),
                "source": c.source,
                "components": c.components,
            },
        )
    if rebate:
        upsert(
            "rebate",
            rebate,
            lambda c: {
                "effective_from": c.effective_from.isoformat(),
                "source": c.source,
                "oer": c.oer,
            },
        )

    lines = [
        "# Generated by `tec rates update`. Do not edit by hand.",
        f"# Fetched: {datetime.now(timezone.utc).isoformat()}",
        "# This overlay is merged over config/rates.toml; same effective dates win.",
        "",
    ]
    for item in sections["commodity"]:
        lines += [
            "[[commodity]]",
            f"effective_from = {item['effective_from']}",
            f"source = {_toml_value(item['source'])}",
            f"tou = {_inline_table(item['tou'])}",
            f"ulo = {_inline_table(item['ulo'])}",
            f"tiered = {_inline_table(item['tiered'])}",
            f"commodity_tier_threshold_summer_kwh = {_fmt_number(item['summer'])}",
            f"commodity_tier_threshold_winter_kwh = {_fmt_number(item['winter'])}",
            "",
        ]
    for item in sections["delivery"]:
        lines += [
            "[[delivery]]",
            f"effective_from = {item['effective_from']}",
            f"source = {_toml_value(item['source'])}",
            f"components = {_inline_table(item['components'])}",
            f"delivery_adjustment = {_fmt_float(item['adjustment'])}",
            f"fixed_per_30_days = {_inline_table(item['fixed'])}",
            "",
        ]
    for item in sections["regulatory"]:
        lines += [
            "[[regulatory]]",
            f"effective_from = {item['effective_from']}",
            f"source = {_toml_value(item['source'])}",
            f"components = {_inline_table(item['components'])}",
            "",
        ]
    for item in sections["rebate"]:
        lines += [
            "[[rebate]]",
            f"effective_from = {item['effective_from']}",
            f"source = {_toml_value(item['source'])}",
            f"oer = {_fmt_float(item['oer'])}",
            "",
        ]
    if hst is not None:
        lines.insert(3, f"hst = {_fmt_float(hst)}")
    if fetched:
        lines.append("# Sources:")
        for page in fetched:
            lines.append(f"#   {page.url} sha256={page.sha256}")

    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_existing_auto(path: Path) -> dict[str, list[dict]]:
    if not path.is_file():
        return {}
    import tomllib

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, list[dict]] = {}
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        cards = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            if key == "commodity":
                cards.append(
                    {
                        "effective_from": str(raw["effective_from"]),
                        "source": raw.get("source", ""),
                        "tou": raw.get("tou", {}),
                        "ulo": raw.get("ulo", {}),
                        "tiered": raw.get("tiered", {}),
                        "summer": raw.get("commodity_tier_threshold_summer_kwh", 600),
                        "winter": raw.get("commodity_tier_threshold_winter_kwh", 1000),
                    }
                )
            elif key == "delivery":
                cards.append(
                    {
                        "effective_from": str(raw["effective_from"]),
                        "source": raw.get("source", ""),
                        "components": raw.get("components", {}),
                        "adjustment": raw.get("delivery_adjustment", 0.0),
                        "fixed": raw.get("fixed_per_30_days", {}),
                    }
                )
            elif key == "regulatory":
                cards.append(
                    {
                        "effective_from": str(raw["effective_from"]),
                        "source": raw.get("source", ""),
                        "components": raw.get("components", {}),
                    }
                )
            elif key == "rebate":
                cards.append(
                    {
                        "effective_from": str(raw["effective_from"]),
                        "source": raw.get("source", ""),
                        "oer": raw.get("oer", 0.0),
                    }
                )
        out[key] = cards
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class UpdateReport:
    changes: list[Change]
    parsed: ParsedRates
    schedule: RateSchedule
    warnings: list[str]
    notes: list[str]
    snapshot_dir: Path | None = None
    pages: list[FetchedPage] = field(default_factory=list)
    stale: bool = False

    @property
    def has_changes(self) -> bool:
        return any(change.changed for change in self.changes)


def run_update(
    config: Config,
    *,
    offline: Path | None = None,
    check_holidays: bool = True,
) -> UpdateReport:
    pages: list[FetchedPage] = []
    warnings: list[str] = []
    notes: list[str] = []

    if offline:
        html = offline.read_text(encoding="utf-8")
        page = FetchedPage(
            url=str(offline),
            text=html,
            fetched_at=datetime.now(timezone.utc),
            sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        )
        pages.append(page)
        oeb_html = None
    else:
        rates_page = fetch_page(RATES_URL)
        pages.append(rates_page)
        html = rates_page.text
        try:
            oeb_page = fetch_page(OEB_HISTORICAL_URL)
            pages.append(oeb_page)
            oeb_html = oeb_page.text
        except RateUpdateError as exc:
            warnings.append(f"OEB cross-check skipped: {exc}")
            oeb_html = None

    parsed = parse_toronto_hydro(html)
    warnings.extend(parsed.warnings)
    notes.extend(parsed.notes)

    schedule = load_rates(config)

    if oeb_html:
        try:
            oeb = parse_oeb_historical(oeb_html)
            cross_check(parsed, oeb, warnings)
        except Exception as exc:  # noqa: BLE001 - never lose the primary update
            warnings.append(f"OEB cross-check failed: {exc}")

    # The distributor page only carries the current season's tier threshold.
    # If the OEB was unreachable, carry forward the stored pair.
    if parsed.commodity and parsed.tier_thresholds is None and schedule.commodity:
        old = _existing_card(schedule.commodity, parsed.commodity.effective_from)
        if old is None:
            old = schedule.commodity[-1]
        parsed.commodity = CommodityCard(
            effective_from=parsed.commodity.effective_from,
            tou=parsed.commodity.tou,
            ulo=parsed.commodity.ulo,
            tiered=parsed.commodity.tiered,
            tier_threshold_summer_kwh=old.tier_threshold_summer_kwh,
            tier_threshold_winter_kwh=old.tier_threshold_winter_kwh,
            source=parsed.commodity.source,
        )
        parsed.tier_thresholds = (
            old.tier_threshold_summer_kwh,
            old.tier_threshold_winter_kwh,
        )
        warnings.append(
            "Tier thresholds carried forward from the stored card; the OEB "
            "cross-check did not provide them."
        )

    if check_holidays and not offline:
        try:
            holiday_page = fetch_page(OEB_HOLIDAYS_URL)
            pages.append(holiday_page)
            official = parse_oeb_holidays(holiday_page.text)
            for year, days in official.items():
                ours = set(computed_holidays(year))
                if ours != days:
                    missing = sorted(days - ours)
                    extra = sorted(ours - days)
                    warnings.append(
                        f"OEB holiday schedule for {year} differs from the built-in "
                        f"calculation (missing={missing}, extra={extra}). "
                        "Update periods.py before billing that year."
                    )
        except RateUpdateError as exc:
            warnings.append(f"holiday cross-check skipped: {exc}")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"holiday cross-check failed: {exc}")

    # Never let a stale page walk history backwards.
    stale = False
    if parsed.commodity and schedule.commodity:
        latest = schedule.commodity[-1].effective_from
        if parsed.commodity.effective_from < latest:
            stale = True
            warnings.append(
                f"Parsed commodity date {parsed.commodity.effective_from} is older "
                f"than the newest stored card {latest}; the update will not be applied."
            )
    if parsed.delivery and schedule.delivery:
        latest = schedule.delivery[-1].effective_from
        if parsed.delivery.effective_from < latest:
            stale = True
            warnings.append(
                f"Parsed delivery date {parsed.delivery.effective_from} is older than "
                f"the newest stored card {latest}; the update will not be applied."
            )

    changes = build_changes(schedule, parsed)
    report = UpdateReport(
        changes=changes,
        parsed=parsed,
        schedule=schedule,
        warnings=warnings,
        notes=notes,
        pages=pages,
        stale=stale,
    )

    if not offline:
        report.snapshot_dir = snapshot(pages, config.root)
    return report


def apply_update(config: Config, report: UpdateReport) -> None:
    if report.stale:
        raise RateUpdateError(
            "refusing to apply a stale page: it is older than the newest stored "
            "rate card. Check the source or update rates.toml deliberately."
        )
    parsed = report.parsed
    write_auto_rates(
        config.auto_rates_path,
        commodity=parsed.commodity,
        delivery=parsed.delivery,
        regulatory=parsed.regulatory,
        rebate=parsed.rebate,
        hst=parsed.hst,
        fetched=report.pages,
    )


def render_report(report: UpdateReport) -> str:
    lines: list[str] = []
    parsed = report.parsed
    lines.append("Rate update check")
    lines.append("=" * 60)
    if parsed.commodity:
        lines.append(f"Commodity card:  effective {parsed.commodity.effective_from}")
    if parsed.delivery:
        lines.append(f"Delivery card:   effective {parsed.delivery.effective_from}")
    if parsed.regulatory:
        lines.append(f"Regulatory card: effective {parsed.regulatory.effective_from}")
    if parsed.rebate:
        lines.append(f"OER:             effective {parsed.rebate.effective_from}")
    lines.append("")

    by_section: dict[str, list[Change]] = {}
    for change in report.changes:
        by_section.setdefault(change.section, []).append(change)

    for section in ("TOU", "ULO", "Tiered", "Delivery", "Delivery (fixed)", "Regulatory", "Rebate"):
        items = by_section.get(section, [])
        if not items:
            continue
        changed = [c for c in items if c.changed]
        header = f"{section}"
        if changed:
            header += f"  ({len(changed)} change{'s' if len(changed) != 1 else ''})"
        else:
            header += "  (unchanged)"
        lines.append(header)
        lines.extend(change.render() for change in items)
        lines.append("")

    if parsed.hst is not None:
        lines.append(f"HST parsed: {parsed.hst:.1%}")
        lines.append("")

    if report.notes:
        lines.append("Notes")
        for note in report.notes:
            lines.append(f"  - {note}")
        lines.append("")
    if report.warnings:
        lines.append("Warnings")
        for warning in report.warnings:
            lines.append(f"  ! {warning}")
        lines.append("")

    if report.snapshot_dir:
        lines.append(f"Snapshot: {report.snapshot_dir}")
        lines.append("")

    if report.has_changes:
        lines.append("Result: changes available.")
    else:
        lines.append("Result: everything matches the published rates.")
    return "\n".join(lines)
