"""JWST observation discovery and archive-record normalization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..config import DiscoveryConfig
from ..datasets import attach_products, build_science_datasets
from ..exceptions import ArchiveQueryError
from ..models import Exposure, Product, ScienceDataset
from .client import MastClient
from .products import normalize_products, select_starting_products

JWST_SEARCH_COLUMNS = (
    "ArchiveFileID",
    "fileSetName",
    "productLevel",
    "program",
    "observtn",
    "visit",
    "visit_id",
    "targprop",
    "targname",
    "targ_ra",
    "targ_dec",
    "instrume",
    "exp_type",
    "tsovisit",
    "opticalElements",
    "filter",
    "pupil",
    "grating",
    "detector",
    "subarray",
    "date_obs",
    "expstart",
    "expend",
    "duration",
    "nints",
    "ngroups",
    "exsegtot",
    "access",
    "pi_name",
    "proposal_type",
)


class ArchiveClient(Protocol):
    """Read-only archive operations needed by discovery."""

    def query_exposures(
        self, criteria: dict[str, Any], select_columns: tuple[str, ...]
    ) -> list[dict[str, Any]]: ...

    def list_products(self, exposure_ids: list[str]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class DiscoveryResult:
    """Hierarchical scientific datasets with flattened convenience views."""

    datasets: tuple[ScienceDataset, ...]

    @property
    def exposures(self) -> tuple[Exposure, ...]:
        """Return all exposure children in dataset order."""
        return tuple(exposure for dataset in self.datasets for exposure in dataset.exposures)

    @property
    def products(self) -> tuple[Product, ...]:
        """Return all segment/product children in exposure order."""
        return tuple(product for exposure in self.exposures for product in exposure.products)


def discover(
    config: DiscoveryConfig,
    client: ArchiveClient | None = None,
) -> DiscoveryResult:
    """Discover matching exposures and organize them without downloading."""
    archive = client or MastClient()
    records = archive.query_exposures(_mast_criteria(config), JWST_SEARCH_COLUMNS)
    normalized = map(normalize_exposure, records)
    exposures = tuple(
        sorted(
            (
                exposure
                for exposure in normalized
                if exposure.instrument == config.query.instrument
                and exposure.exposure_type == config.query.exposure_type
            ),
            key=_exposure_sort_key,
        )
    )
    if not exposures:
        raise ArchiveQueryError("MAST returned no matching JWST exposure datasets.")

    exposure_ids = [exposure.exposure_id for exposure in exposures]
    if any(exposure_id is None for exposure_id in exposure_ids):
        raise ArchiveQueryError("MAST returned an exposure without fileSetName.")

    product_records = archive.list_products([str(exposure_id) for exposure_id in exposure_ids])
    products = select_starting_products(normalize_products(product_records), config.start_from)
    if not products:
        raise ArchiveQueryError(
            f"MAST returned no science _{config.start_from} products for the matched exposures."
        )
    exposures_with_products = attach_products(exposures, products)
    return DiscoveryResult(datasets=build_science_datasets(exposures_with_products))


def _mast_criteria(config: DiscoveryConfig) -> dict[str, Any]:
    """Translate the configured target strategy at the archive boundary."""
    query = config.query
    criteria: dict[str, Any] = {
        "targname": ",".join(query.archive_target_names),
        "instrume": query.instrument,
        "exp_type": query.exposure_type,
        "productLevel": "1b",
    }
    if query.proposal_ids is not None:
        criteria["program"] = ",".join(query.proposal_ids)
    if query.observation_id is not None:
        criteria["observtn"] = query.observation_id
    if query.visit_id is not None:
        criteria["visit_id"] = query.visit_id
    return criteria


def normalize_exposure(record: dict[str, Any]) -> Exposure:
    """Normalize one exposure-level JWST mission-search row."""
    return Exposure(
        program_id=_identifier(record.get("program"), width=5),
        observation_id=_identifier(record.get("observtn"), width=3),
        visit_id=_identifier(record.get("visit_id"), width=11),
        target_name=_text(record.get("targname")),
        instrument=_upper_text(record.get("instrume")),
        exposure_type=_upper_text(record.get("exp_type")),
        archive_id=_text(record.get("ArchiveFileID")),
        exposure_id=_required_text(record, "fileSetName"),
        visit_number=_identifier(record.get("visit"), width=3),
        is_tso=_boolean(record.get("tsovisit")),
        optical_elements=_text(record.get("opticalElements")),
        detector=_upper_text(record.get("detector")),
        grating=_nirspec_grating(record),
        filter=_upper_text(record.get("filter")),
        subarray=_upper_text(record.get("subarray")),
        start_time=_text(record.get("date_obs")),
        duration_seconds=_float(record.get("duration")),
        integration_count=_integer(record.get("nints")),
        group_count=_integer(record.get("ngroups")),
        segment_count=_integer(record.get("exsegtot")),
        access=_upper_text(record.get("access")),
        metadata=dict(record),
    )


def _required_text(record: dict[str, Any], field: str) -> str:
    value = _text(record.get(field))
    if value is None:
        raise ArchiveQueryError(f"MAST dataset metadata is missing required field '{field}'.")
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _upper_text(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text is not None else None


def _identifier(value: Any, *, width: int) -> str | None:
    text = _text(value)
    return text.zfill(width) if text is not None else None


def _integer(value: Any) -> int | None:
    return None if value is None else int(value)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _boolean(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"t", "true", "1", "yes"}:
        return True
    if normalized in {"f", "false", "0", "no"}:
        return False
    raise ArchiveQueryError(f"Unrecognized MAST boolean value: {value!r}")


def _nirspec_grating(record: dict[str, Any]) -> str | None:
    """Normalize the disperser from explicit or combined MAST optical metadata."""
    explicit = _upper_text(record.get("grating"))
    if explicit is not None:
        return explicit
    if _upper_text(record.get("instrume")) != "NIRSPEC":
        return None
    filter_name = _upper_text(record.get("filter"))
    elements = (
        _upper_text(element)
        for element in str(record.get("opticalElements") or "").split(";")
    )
    return next(
        (element for element in elements if element is not None and element != filter_name),
        None,
    )


def _exposure_sort_key(exposure: Exposure) -> tuple[str, ...]:
    return (
        exposure.program_id or "",
        exposure.observation_id or "",
        exposure.visit_number or "",
        exposure.exposure_id or "",
    )
