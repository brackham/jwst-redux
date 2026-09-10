"""Mode-aware construction of scientific datasets from archive exposures."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace

from .exceptions import ArchiveQueryError
from .models import Exposure, Product, ScienceDataset

# A scientific dataset boundary is observing-mode dependent.  NIRISS/SOSS time
# series are identified by JWST program, observation, and visit number.  Future
# modes should add their own explicit identity rule rather than inheriting this one.
DATASET_IDENTITY_FIELDS_BY_MODE = {
    ("NIRISS", "NIS_SOSS"): ("program_id", "observation_id", "visit_number"),
}


def attach_products(
    exposures: tuple[Exposure, ...], products: tuple[Product, ...]
) -> tuple[Exposure, ...]:
    """Attach each selected segment/product to its parent archive exposure."""
    exposure_ids = {exposure.exposure_id for exposure in exposures}
    orphan_ids = {
        product.exposure_id for product in products if product.exposure_id not in exposure_ids
    }
    if orphan_ids:
        raise ArchiveQueryError(
            "MAST returned starting products for unknown exposures: "
            + ", ".join(sorted(str(value) for value in orphan_ids))
        )

    attached = []
    for exposure in exposures:
        children = tuple(
            product for product in products if product.exposure_id == exposure.exposure_id
        )
        if not children:
            raise ArchiveQueryError(
                f"MAST returned no science _uncal products for exposure {exposure.exposure_id}."
            )
        attached.append(replace(exposure, products=children))
    return tuple(attached)


def build_science_datasets(exposures: Iterable[Exposure]) -> tuple[ScienceDataset, ...]:
    """Aggregate exposure records using the identity rule for their observing mode."""
    grouped: dict[
        tuple[tuple[str, str], tuple[tuple[str, str], ...]], list[Exposure]
    ] = defaultdict(list)
    for exposure in exposures:
        mode = (exposure.instrument or "", exposure.exposure_type or "")
        identity = _dataset_identity(exposure)
        grouped[(mode, identity)].append(exposure)

    datasets = []
    for (mode, identity), members in sorted(grouped.items()):
        ordered = tuple(sorted(members, key=lambda item: item.exposure_id or ""))
        datasets.append(
            ScienceDataset(
                dataset_id="-".join((*mode, *(value for _, value in identity))).lower(),
                identity=identity,
                exposures=ordered,
            )
        )
    return tuple(datasets)


def _dataset_identity(exposure: Exposure) -> tuple[tuple[str, str], ...]:
    mode = (exposure.instrument, exposure.exposure_type)
    fields = DATASET_IDENTITY_FIELDS_BY_MODE.get(mode)
    if fields is None:
        raise ArchiveQueryError(
            "No scientific dataset identity rule is defined for "
            f"instrument={exposure.instrument!r}, exposure_type={exposure.exposure_type!r}."
        )

    identity = tuple((field, _identity_value(exposure, field)) for field in fields)
    missing = [field for field, value in identity if not value]
    if missing:
        raise ArchiveQueryError(
            f"Exposure {exposure.exposure_id} lacks scientific dataset identity metadata: "
            + ", ".join(missing)
        )
    return identity


def _identity_value(exposure: Exposure, field: str) -> str:
    value = getattr(exposure, field)
    return "" if value is None else str(value)
