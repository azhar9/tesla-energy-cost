from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tesla_energy_cost.config import load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture()
def project(tmp_path: Path):
    """A disposable project root with the committed config copied in."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("config.toml", "rates.toml"):
        shutil.copy(ROOT / "config" / name, config_dir / name)
    return load_config(tmp_path)


@pytest.fixture()
def fixtures() -> Path:
    return FIXTURES
