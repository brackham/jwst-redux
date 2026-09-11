"""Read-only wrapper around the JWST mission interface in ``astroquery.mast``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from astroquery.mast import MastMissions


class MissionsBackend(Protocol):
    """Subset of ``MastMissions`` used by this milestone."""

    def query_criteria(self, **kwargs: Any) -> Any:
        """Return mission search rows."""

    def get_product_list(self, datasets: list[str]) -> Any:
        """Return product rows for exposure-level MAST dataset identifiers."""

    def download_file(
        self, uri: str, *, local_path: Path, cache: bool, verbose: bool
    ) -> tuple[str, str | None, str | None]:
        """Download one archive product to an explicit local path."""


class MastClient:
    """Expose MAST query results as plain records and never download data."""

    def __init__(self, backend: MissionsBackend | None = None) -> None:
        self._backend = backend or MastMissions(mission="jwst")

    def query_exposures(
        self, criteria: dict[str, Any], select_columns: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Query JWST exposure records using mission-specific archive fields."""
        table = self._backend.query_criteria(
            **criteria,
            select_cols=list(select_columns),
        )
        return _table_records(table)

    def list_products(self, exposure_ids: list[str]) -> list[dict[str, Any]]:
        """List products for exposure-level MAST IDs without retrieving files."""
        table = self._backend.get_product_list(exposure_ids)
        return _table_records(table)

    def download_product(self, uri: str, destination: Path) -> tuple[str, str | None, str | None]:
        """Download one product with Astroquery's supported MAST machinery."""
        return self._backend.download_file(
            uri,
            local_path=destination,
            cache=False,
            verbose=True,
        )


def _table_records(table: Any) -> list[dict[str, Any]]:
    """Convert an Astropy-like table into builtin Python records."""
    columns = tuple(table.colnames)
    return [{column: _plain_value(row[column]) for column in columns} for row in table]


def _plain_value(value: Any) -> Any:
    if getattr(value, "mask", False) is True:
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            pass
    return value
