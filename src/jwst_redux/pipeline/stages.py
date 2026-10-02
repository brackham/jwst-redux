"""Thin metadata mappings to official ``jwst`` pipeline classes."""

from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import PlanningError
from ..models import Exposure
from ..modes import BOTS_MODE, SOSS_MODE, mode_key, nirspec_bots_detectors, observing_mode_for


@dataclass(frozen=True)
class PipelinePath:
    """Supported official pipeline classes and any archive-relevant limitation."""

    classes: tuple[str, ...]
    note: str | None = None


def pipeline_path_for(exposure: Exposure) -> PipelinePath:
    """Resolve a supported official pipeline path from normalized metadata."""
    try:
        definition = observing_mode_for(exposure)
    except PlanningError as error:
        raise PlanningError(
            "Only NIRISS/NIS_SOSS and NIRSpec/NRS_BRIGHTOBJ or NRS_FIXEDSLIT science "
            "observations are supported."
        ) from error
    if definition.is_tso and exposure.is_tso is not True:
        raise PlanningError(
            "Only supported TSO science observations with TSOVISIT=true can be planned."
        )
    if not definition.is_tso and exposure.is_tso is True:
        raise PlanningError("Non-TSO spectroscopy requires TSOVISIT=false.")
    mode = mode_key(exposure)
    if mode == SOSS_MODE:
        return _soss_pipeline_path(exposure)
    if mode == BOTS_MODE:
        return _bots_pipeline_path(exposure)
    return PipelinePath(classes=("Detector1Pipeline", "Spec2Pipeline", definition.stage3_class))


def _soss_pipeline_path(exposure: Exposure) -> PipelinePath:
    """Preserve the validated NIRISS/SOSS mode-specific limitations."""

    optical_elements = {
        element.strip().upper()
        for element in (exposure.optical_elements or "").split(";")
        if element.strip()
    }
    if not optical_elements:
        raise PlanningError(f"Exposure {exposure.exposure_id} has no optical-elements metadata.")
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
    return PipelinePath(classes=("Detector1Pipeline", "Spec2Pipeline", "Tso3Pipeline"))


def _bots_pipeline_path(exposure: Exposure) -> PipelinePath:
    """Resolve the standard official NIRSpec/BOTS TSO pipeline path."""
    nirspec_bots_detectors(exposure)
    integrations = exposure.integration_count
    if integrations is None or integrations < 1:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} has invalid or missing NINTS metadata."
        )
    return PipelinePath(classes=("Detector1Pipeline", "Spec2Pipeline", "Tso3Pipeline"))
