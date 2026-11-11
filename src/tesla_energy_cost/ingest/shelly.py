"""Local Shelly meter readers (Gen2/Gen3 RPC API)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ShellyError(RuntimeError):
    pass


@dataclass
class ShellyReading:
    counter_wh: float | None
    power_w: float | None
    device: str
    raw: dict[str, Any]


def _get(base: str, path: str, timeout: float) -> dict[str, Any]:
    url = base.rstrip("/") + path
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ShellyError(f"cannot reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ShellyError(f"{url} did not return JSON") from exc
    except TimeoutError as exc:
        raise ShellyError(f"timeout reaching {url}") from exc


def _sum_phases(data: dict[str, Any]) -> float | None:
    # Prefer the device's own aggregate; summing it together with the phase
    # values would double-count. Only fall back to phases when no total exists.
    for key in ("total_act_energy", "total_act"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    values = [
        data.get(key)
        for key in ("a_total_act_energy", "b_total_act_energy", "c_total_act_energy")
    ]
    present = [float(v) for v in values if isinstance(v, (int, float))]
    return sum(present) if present else None


def read(host: str, *, timeout: float = 5.0) -> ShellyReading:
    """Read a Shelly Plug / EM / Pro EM over the local network.

    The cumulative energy counter is preferred: it is exact even if a poll is
    missed. Power-only devices are integrated by the caller.
    """
    base = host if host.startswith(("http://", "https://")) else f"http://{host}"
    status = _get(base, "/rpc/Shelly.GetStatus", timeout)

    # Shelly Plus Plug US / Plus / Gen3 switch component
    for key, value in status.items():
        if key.startswith("switch:") and isinstance(value, dict):
            energy = value.get("aenergy") or {}
            counter = energy.get("total")
            power = value.get("apower")
            if counter is not None or power is not None:
                return ShellyReading(
                    counter_wh=float(counter) if counter is not None else None,
                    power_w=float(power) if power is not None else None,
                    device=str(status.get("sys", {}).get("device", {}).get("name", key)),
                    raw=status,
                )

    # Shelly EM / Pro EM: cumulative energy lives in EMData
    for key, value in status.items():
        if key.startswith("em:") and isinstance(value, dict):
            power = value.get("total_act_power")
            if power is None:
                power = value.get("a_act_power")
            channel = key.split(":", 1)[1]
            counter = None
            try:
                emdata = _get(base, f"/rpc/EMData.GetStatus?id={channel}", timeout)
                counter = _sum_phases(emdata)
            except ShellyError:
                counter = _sum_phases(value)
            return ShellyReading(
                counter_wh=float(counter) if counter is not None else None,
                power_w=float(power) if power is not None else None,
                device=str(status.get("sys", {}).get("device", {}).get("name", key)),
                raw=status,
            )

    # Newer EM1 component (single-phase energy meter)
    for key, value in status.items():
        if key.startswith("em1:") and isinstance(value, dict):
            channel = key.split(":", 1)[1]
            power = value.get("act_power")
            try:
                emdata = _get(base, f"/rpc/EM1Data.GetStatus?id={channel}", timeout)
                counter = emdata.get("act_energy") or emdata.get("total_act_energy")
            except ShellyError:
                counter = None
            return ShellyReading(
                counter_wh=float(counter) if counter is not None else None,
                power_w=float(power) if power is not None else None,
                device=str(status.get("sys", {}).get("device", {}).get("name", key)),
                raw=status,
            )

    raise ShellyError(
        "no switch/em/em1 component found. This tool supports Shelly Gen2/Gen3 "
        "devices (Plus Plug US, Plus Plug, EM, Pro EM). The original Shelly Plug "
        "US is Gen1 and is not supported; export a CSV instead."
    )
