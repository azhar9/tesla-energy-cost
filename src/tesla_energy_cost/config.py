"""Project configuration loading (config/config.toml + config/local.toml)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward looking for the repository root."""
    env = os.environ.get("TEC_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "rates.toml").is_file():
            return candidate
    # Also support being run from a subdirectory of an installed checkout
    # where config was passed explicitly via TEC_ROOT.
    raise ConfigError(
        "Cannot find config/rates.toml. Run `tec init` first, or set TEC_ROOT "
        "to the project directory."
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


class Config:
    def __init__(self, root: Path):
        self.root = root
        base = _load_toml(root / "config" / "config.toml")
        local = _load_toml(root / "config" / "local.toml")
        self.data = _deep_merge(base, local)
        self.rates_path = root / "config" / "rates.toml"
        self.local_rates_path = root / "config" / "rates.local.toml"
        self.auto_rates_path = root / "config" / "rates.auto.toml"

    # -- convenience accessors -------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.get("timezone", "America/Toronto"))

    @property
    def currency(self) -> str:
        return self.get("currency", "CAD")

    @property
    def plan(self) -> str:
        plan = str(self.get("billing.plan", "tou")).lower()
        if plan not in {"tou", "ulo", "tiered"}:
            raise ConfigError(f"billing.plan must be tou/ulo/tiered, got {plan!r}")
        return plan

    @property
    def method(self) -> str:
        method = str(self.get("billing.method", "marginal")).lower()
        if method not in {"marginal", "proportional"}:
            raise ConfigError(
                f"billing.method must be marginal/proportional, got {method!r}"
            )
        return method

    @property
    def source(self) -> str:
        return str(self.get("meter.source", "shelly-plug"))

    @property
    def assume_period(self) -> str | None:
        value = str(self.get("meter.assume_period_for_coarse_data", "") or "").strip()
        return value or None

    @property
    def storage_path(self) -> Path:
        return (self.root / str(self.get("storage.path", "data/energy.db"))).resolve()

    @property
    def statement_dir(self) -> Path:
        return (self.root / str(self.get("billing.statement_dir", "statements"))).resolve()

    @property
    def household_kwh(self) -> float | None:
        value = self.get("billing.household_monthly_kwh")
        return float(value) if value is not None else None

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (self.root / path).resolve()


def load_config(root: Path | None = None) -> Config:
    return Config(root or find_project_root())
