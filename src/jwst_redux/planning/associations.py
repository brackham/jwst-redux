"""Compatible reduction branches within a scientific dataset."""

from __future__ import annotations

from collections import defaultdict

from ..exceptions import PlanningError
from ..models import Exposure, Product, ReductionGroup, ScienceDataset
from ..pipeline.stages import pipeline_path_for

# These normalized fields represent the official TSO association constraints available
# from the current MAST mission search: observation candidate, target, mode/TSO state,
# and optical path. Visit is deliberately not a universal grouping rule.
COMPATIBILITY_FIELDS = (
    "program_id",
    "observation_id",
    "target_name",
    "instrument",
    "exposure_type",
    "is_tso",
    "optical_elements",
    "subarray",
    "pipeline_path",
)


def build_compatible_groups(dataset: ScienceDataset) -> tuple[ReductionGroup, ...]:
    """Group a dataset's exposures by pipeline compatibility and validate segments."""
    exposure_groups: dict[tuple[str, ...], list[Exposure]] = defaultdict(list)
    for exposure in dataset.exposures:
        exposure_groups[_compatibility_key(exposure)].append(exposure)

    groups = []
    for key, members in sorted(exposure_groups.items()):
        ordered_members = tuple(sorted(members, key=_exposure_sort_key))
        group_products = []
        for exposure in ordered_members:
            if exposure.exposure_id is None:
                raise PlanningError("Exposure has no archive identifier.")
            exposure_products = tuple(sorted(exposure.products, key=_product_sort_key))
            if not exposure_products:
                raise PlanningError(
                    f"No starting products found for exposure {exposure.exposure_id}."
                )
            _validate_segments(exposure, exposure_products)
            group_products.extend(exposure_products)

        compatibility = tuple(zip(COMPATIBILITY_FIELDS, key, strict=True))
        groups.append(
            ReductionGroup(
                group_id=" + ".join(
                    exposure.exposure_id or "unknown" for exposure in ordered_members
                ),
                dataset_id=dataset.dataset_id,
                compatibility=compatibility,
                exposures=ordered_members,
                products=tuple(group_products),
            )
        )
    return tuple(groups)


def _compatibility_key(exposure: Exposure) -> tuple[str, ...]:
    metadata_fields = COMPATIBILITY_FIELDS[:-1]
    values = tuple(_compatibility_value(getattr(exposure, field)) for field in metadata_fields)
    values += (" -> ".join(pipeline_path_for(exposure).classes),)
    if any(not value for value in values):
        missing = [
            field for field, value in zip(COMPATIBILITY_FIELDS, values, strict=True) if not value
        ]
        raise PlanningError(
            "Cannot determine association compatibility; missing metadata: " + ", ".join(missing)
        )
    return values


def _validate_segments(exposure: Exposure, products: tuple[Product, ...]) -> None:
    expected = exposure.segment_count
    segment_numbers = [product.segment_number for product in products]

    if expected is None:
        if any(segment is not None for segment in segment_numbers):
            raise PlanningError(
                f"Exposure {exposure.exposure_id} has segmented products but no EXSEGTOT metadata."
            )
        if len(products) != 1:
            raise PlanningError(
                f"Exposure {exposure.exposure_id} has {len(products)} unsegmented starting products."
            )
        return

    if expected < 1:
        raise PlanningError(f"Exposure {exposure.exposure_id} has invalid EXSEGTOT={expected}.")
    if expected == 1 and len(products) == 1 and segment_numbers[0] in {None, 1}:
        return
    if any(segment is None for segment in segment_numbers):
        raise PlanningError(
            f"Exposure {exposure.exposure_id} is segmented but a product lacks '-segNNN'."
        )
    actual = [int(segment) for segment in segment_numbers if segment is not None]
    if len(actual) != len(set(actual)):
        raise PlanningError(f"Exposure {exposure.exposure_id} has duplicate segment numbers.")
    expected_numbers = list(range(1, expected + 1))
    if sorted(actual) != expected_numbers:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} has segments {sorted(actual)}; "
            f"expected {expected_numbers}."
        )


def _compatibility_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return "" if value is None else str(value)


def _exposure_sort_key(exposure: Exposure) -> tuple[str, ...]:
    return (exposure.exposure_id or "",)


def _product_sort_key(product: Product) -> tuple[int, str]:
    segment = product.segment_number if product.segment_number is not None else 0
    return (segment, product.filename)
