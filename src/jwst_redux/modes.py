"""Central observing-mode definitions used across discovery and planning."""

from __future__ import annotations

from dataclasses import dataclass

from .exceptions import PlanningError
from .models import Exposure

SOSS_MODE = ("NIRISS", "NIS_SOSS")
BOTS_MODE = ("NIRSPEC", "NRS_BRIGHTOBJ")


@dataclass(frozen=True)
class ObservingMode:
    """Metadata contract for one supported official JWST TSO mode."""

    label: str
    dataset_identity_fields: tuple[str, ...]
    compatibility_fields: tuple[str, ...]
    detector_branches: bool = False


SUPPORTED_MODES: dict[tuple[str, str], ObservingMode] = {
    SOSS_MODE: ObservingMode(
        label="NIRISS/SOSS",
        dataset_identity_fields=("program_id", "observation_id", "visit_number"),
        compatibility_fields=("optical_elements", "subarray"),
    ),
    BOTS_MODE: ObservingMode(
        label="NIRSpec/BOTS",
        dataset_identity_fields=("program_id", "observation_id", "visit_number"),
        compatibility_fields=("detector", "grating", "filter", "subarray"),
        detector_branches=True,
    ),
}

# These BOTS configurations place useful spectrum on both detector arrays.  The
# remaining supported BOTS disperser/filter combinations fall entirely on NRS1.
# See the STScI NIRSpec BOTS wavelength-ranges and detector-gap documentation.
NIRSPEC_TWO_DETECTOR_CONFIGURATIONS = {
    ("G140H", "F100LP"),
    ("G235H", "F170LP"),
    ("G395H", "F290LP"),
}


def mode_key(exposure: Exposure) -> tuple[str, str]:
    """Return normalized instrument/exposure-type identity."""
    return (exposure.instrument or "", exposure.exposure_type or "")


def observing_mode_for(exposure: Exposure) -> ObservingMode:
    """Return the supported mode definition or fail before any write occurs."""
    key = mode_key(exposure)
    try:
        return SUPPORTED_MODES[key]
    except KeyError as error:
        raise PlanningError(
            "No supported science mode is defined for "
            f"instrument={exposure.instrument!r}, exposure_type={exposure.exposure_type!r}."
        ) from error


def nirspec_bots_detectors(exposure: Exposure) -> tuple[str, ...]:
    """Return detectors containing useful BOTS spectra for this optical setup."""
    if mode_key(exposure) != BOTS_MODE:
        raise PlanningError("NIRSpec detector resolution requires an NRS_BRIGHTOBJ exposure.")
    if not exposure.grating or not exposure.filter:
        raise PlanningError(
            f"Exposure {exposure.exposure_id} lacks NIRSpec grating/filter metadata."
        )
    if not exposure.subarray:
        raise PlanningError(f"Exposure {exposure.exposure_id} lacks NIRSpec subarray metadata.")
    configuration = (exposure.grating, exposure.filter)
    if configuration in NIRSPEC_TWO_DETECTOR_CONFIGURATIONS:
        return ("NRS1", "NRS2")
    return ("NRS1",)
