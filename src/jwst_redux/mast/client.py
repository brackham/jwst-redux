"""Read-only wrapper around the JWST mission interface in ``astroquery.mast``."""

from __future__ import annotations

import io
import logging
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Protocol

from astroquery.mast import MastMissions

from ..exceptions import ArchiveQueryError
from ..security import redact


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
        self._backend = backend or self._request(MastMissions, mission="jwst")
        if os.environ.get("MAST_API_TOKEN"):
            self._request(
                self._backend.login, token=os.environ["MAST_API_TOKEN"], store_token=False
            )

    def query_exposures(
        self, criteria: dict[str, Any], select_columns: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Query JWST exposure records using mission-specific archive fields."""
        table = self._request(
            self._backend.query_criteria,
            **criteria,
            select_cols=list(select_columns),
        )
        return _table_records(table)

    def list_products(self, exposure_ids: list[str]) -> list[dict[str, Any]]:
        """List products for exposure-level MAST IDs without retrieving files."""
        table = self._request(self._backend.get_product_list, exposure_ids)
        return _table_records(table)

    def download_product(self, uri: str, destination: Path) -> tuple[str, str | None, str | None]:
        """Download one product with Astroquery's supported MAST machinery."""
        return redact(
            self._request(
                self._backend.download_file,
                uri,
                local_path=destination,
                cache=False,
                verbose=not bool(os.environ.get("MAST_API_TOKEN")),
            )
        )

    @staticmethod
    def _request(method: Any, *args: Any, **kwargs: Any) -> Any:
        # Astroquery/requests can log request headers at debug level or include
        # them in an exception. Suppress credential-bearing transport diagnostics
        # and expose only a safe exception type at this boundary.
        if not os.environ.get("MAST_API_TOKEN"):
            return method(*args, **kwargs)
        previous = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return method(*args, **kwargs)
        except Exception:  # noqa: BLE001 - credential-bearing transport errors must not escape.
            raise ArchiveQueryError(
                "Authenticated MAST request failed; check access and token validity."
            ) from None
        finally:
            logging.disable(previous)


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
