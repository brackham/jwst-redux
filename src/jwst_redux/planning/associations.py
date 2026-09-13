"""Compatible reduction branches within a scientific dataset."""

from __future__ import annotations

from collections import defaultdict

from ..exceptions import PlanningError
from ..models import Exposure, Product, ReductionGroup, ScienceDataset
from ..modes import nirspec_bots_detectors, observing_mode_for
from ..pipeline.stages import pipeline_path_for

# These normalized fields represent the official TSO association constraints available
# from the current MAST mission search: observation candidate, target, mode/TSO state,
# and optical path. Visit is deliberately not a universal grouping rule.
GENERIC_COMPATIBILITY_FIELDS = (
    "program_id",
    "observation_id",
    "target_name",
    "instrument",
    "exposure_type",
    "is_tso",
)


def build_compatible_groups(dataset: ScienceDataset) -> tuple[ReductionGroup, ...]:
    """Group a dataset's exposures by pipeline compatibility and validate segments."""
    exposure_groups: dict[
        tuple[tuple[str, str], ...], list[tuple[Exposure, tuple[Product, ...]]]
    ] = defaultdict(list)
    for exposure in dataset.exposures:
        for detector, products in _product_branches(exposure):
            exposure_groups[_compatibility_key(exposure, detector)].append(
                (exposure, products)
            )

    groups = []
    for compatibility, members in sorted(exposure_groups.items()):
        ordered_pairs = tuple(sorted(members, key=lambda item: _exposure_sort_key(item[0])))
        ordered_members = tuple(exposure for exposure, _ in ordered_pairs)
        group_products = []
        for exposure, exposure_products in ordered_pairs:
            if exposure.exposure_id is None:
                raise PlanningError("Exposure has no archive identifier.")
            exposure_products = tuple(sorted(exposure_products, key=_product_sort_key))
            if not exposure_products:
                raise PlanningError(
                    f"No starting products found for exposure {exposure.exposure_id}."
                )
            validate_exposure_segments(exposure, exposure_products)
            group_products.extend(exposure_products)

        detector = dict(compatibility).get("detector")
        member_labels = [
            f"{exposure.exposure_id}:{detector.lower()}"
            if detector
            else exposure.exposure_id or "unknown"
            for exposure in ordered_members
        ]
        groups.append(
            ReductionGroup(
                group_id=" + ".join(member_labels),
                dataset_id=dataset.dataset_id,
                compatibility=compatibility,
                exposures=ordered_members,
                products=tuple(group_products),
            )
        )
    return tuple(groups)


def _compatibility_key(
    exposure: Exposure, detector: str | None
) -> tuple[tuple[str, str], ...]:
    mode = observing_mode_for(exposure)
    metadata = {field: getattr(exposure, field) for field in GENERIC_COMPATIBILITY_FIELDS}
    metadata.update(
        {
            field: detector if field == "detector" else getattr(exposure, field)
            for field in mode.compatibility_fields
        }
    )
    metadata["pipeline_path"] = " -> ".join(pipeline_path_for(exposure).classes)
    values = tuple((field, _compatibility_value(value)) for field, value in metadata.items())
    if any(not value for _, value in values):
        missing = [field for field, value in values if not value]
        raise PlanningError(
            "Cannot determine association compatibility; missing metadata: " + ", ".join(missing)
        )
    return values


def _product_branches(exposure: Exposure) -> tuple[tuple[str | None, tuple[Product, ...]], ...]:
    """Partition starting products into mode-appropriate detector branches."""
    mode = observing_mode_for(exposure)
    if not mode.detector_branches:
        return ((None, exposure.products),)

    by_detector: dict[str, list[Product]] = defaultdict(list)
    for product in exposure.products:
        detector = product.detector or exposure.detector
        if detector is None:
            raise PlanningError(
                f"NIRSpec product {product.filename} lacks detector metadata."
            )
        by_detector[detector].append(product)

    required = nirspec_bots_detectors(exposure)
    missing = [detector for detector in required if detector not in by_detector]
    if missing:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} lacks starting products for required detector(s): "
            + ", ".join(missing)
        )
    return tuple((detector, tuple(by_detector[detector])) for detector in required)


def validate_exposure_segments(exposure: Exposure, products: tuple[Product, ...]) -> None:
    """Require one unambiguous starting product for each expected exposure segment."""
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
