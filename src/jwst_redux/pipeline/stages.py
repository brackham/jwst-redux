"""Thin metadata mappings to official ``jwst`` pipeline classes."""

from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import PlanningError
from ..models import Exposure


@dataclass(frozen=True)
class PipelinePath:
    """Supported official pipeline classes and any archive-relevant limitation."""

    classes: tuple[str, ...]
    note: str | None = None


def pipeline_path_for(exposure: Exposure) -> PipelinePath:
    """Resolve the validated NIRISS/SOSS path from normalized metadata."""
    if (
        exposure.instrument != "NIRISS"
        or exposure.exposure_type != "NIS_SOSS"
        or exposure.is_tso is not True
    ):
        raise PlanningError(
            "Only NIRISS NIS_SOSS observations with TSOVISIT=true are currently supported."
        )

    optical_elements = {
        element.strip().upper()
        for element in (exposure.optical_elements or "").split(";")
        if element.strip()
    }
    if not optical_elements:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} has no optical-elements metadata."
        )
    if "F277W" in optical_elements:
        return PipelinePath(
            classes=("Detector1Pipeline",),
            note=(
                "F277W+GR700XD SOSS has no supported pipeline spectral extraction/flux "
                "calibration; official processing stops after Detector1Pipeline."
            ),
        )

    integrations = exposure.integration_count
    if integrations is None or integrations < 1:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} has invalid or missing NINTS metadata."
        )
    if integrations == 1:
        return PipelinePath(
            classes=("Detector1Pipeline", "Spec2Pipeline"),
            note=(
                "Single-integration NIRISS/SOSS exposures are excluded from official "
                "Tso3 association generation; processing stops after Spec2Pipeline."
            ),
        )
    return PipelinePath(
        classes=("Detector1Pipeline", "Spec2Pipeline", "Tso3Pipeline")
    )
