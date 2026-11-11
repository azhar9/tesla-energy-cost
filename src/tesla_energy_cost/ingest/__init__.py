"""Data ingestion adapters: CSV, Shelly local API, Tesla Fleet API."""

from __future__ import annotations

from .csvio import ImportResult, detect_format, import_csv

__all__ = ["ImportResult", "detect_format", "import_csv"]
