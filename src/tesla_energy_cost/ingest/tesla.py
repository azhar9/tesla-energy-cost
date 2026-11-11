"""Tesla Fleet API adapter (optional, approximate).

This module exists for people who want a *trend* without buying a meter. It is
**not billing-grade**:

* ``charger_power`` is sampled when the poller runs. A missed poll is lost
  energy; a 5-minute poll can still miss short charging sessions.
* The car reports energy at the charger/battery boundary, not necessarily the
  wall. Charging losses mean wall energy is typically a few percent higher.
* Tesla may put the vehicle to sleep; this adapter never wakes it.

For a landlord statement, use a physical meter (Shelly Plug / Shelly EM) or
manual cumulative readings. See the README for hardware guidance.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

DEFAULT_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
TOKEN_ENV = "TESLA_ACCESS_TOKEN"
UTC = timezone.utc


class TeslaError(RuntimeError):
    pass


@dataclass
class TeslaReading:
    timestamp: datetime
    charger_power_kw: float | None
    charge_energy_added_kwh: float | None
    charging_state: str
    battery_level: int | None
    raw: dict[str, Any]


def resolve_token(token: str | None = None) -> str:
    value = (token or os.environ.get(TOKEN_ENV, "")).strip()
    if not value:
        raise TeslaError(
            f"no Tesla access token. Set {TOKEN_ENV} to a Fleet API access token, "
            "or use a Shelly meter instead."
        )
    return value


def read(
    vehicle_id: str,
    *,
    token: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
) -> TeslaReading:
    """Read ``charge_state`` for one vehicle.

    Parameters
    ----------
    vehicle_id:
        The Fleet API vehicle id (numeric string), not the VIN.
    token:
        OAuth access token. Falls back to ``TESLA_ACCESS_TOKEN``.
    base_url:
        Regional Fleet API host. Defaults to the North America host.
    """
    if not vehicle_id.strip():
        raise TeslaError("vehicle id is empty; set tesla.vehicle_id in config/local.toml")
    token = resolve_token(token)
    base = (base_url or os.environ.get("TESLA_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
    url = (
        f"{base}/api/1/vehicles/{urllib.parse.quote(vehicle_id.strip())}/vehicle_data"
        "?endpoints=charge_state"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "tesla-energy-cost/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TeslaError(
            f"Tesla Fleet API returned HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise TeslaError(f"cannot reach Tesla Fleet API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise TeslaError("timeout reaching Tesla Fleet API") from exc
    except json.JSONDecodeError as exc:
        raise TeslaError("Tesla Fleet API did not return JSON") from exc

    if "error" in payload and payload["error"]:
        raise TeslaError(f"Tesla Fleet API error: {payload['error']}")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise TeslaError("Tesla Fleet API response has no 'response' object")
    charge = response.get("charge_state")
    if not isinstance(charge, dict):
        raise TeslaError(
            "vehicle data has no charge_state. The car may be asleep; wake it in "
            "the Tesla app and poll again. This tool does not wake vehicles."
        )

    raw_ts = response.get("timestamp") or charge.get("timestamp")
    timestamp = _parse_timestamp(raw_ts)

    power = charge.get("charger_power")
    energy = charge.get("charge_energy_added")
    level = charge.get("battery_level")

    return TeslaReading(
        timestamp=timestamp,
        charger_power_kw=float(power) if isinstance(power, (int, float)) else None,
        charge_energy_added_kwh=(
            float(energy) if isinstance(energy, (int, float)) else None
        ),
        charging_state=str(charge.get("charging_state", "")),
        battery_level=int(level) if isinstance(level, (int, float)) else None,
        raw=response,
    )


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        # Tesla sometimes reports milliseconds since the epoch.
        seconds = float(value) / (1000.0 if value > 10_000_000_000 else 1.0)
        return datetime.fromtimestamp(seconds, UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
