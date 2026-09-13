"""Generic extracted-time-series checks shared by supported spectroscopy modes."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import numpy as np

from ..modes import NIRSPEC_TWO_DETECTOR_CONFIGURATIONS
from .common import (
    SPECTRAL_QA_CONFIG,
    Spectrum,
    native_spectral_stack,
    spectra_from_x1dints,
    spectral_qa_selection,
    valid_flux,
)


def extracted_spectrum_report(
    path: Path,
    *,
    expected_instrument: str,
    expected_exposure_type: str,
    expected_detector: str | None,
) -> dict[str, Any]:
    """Inspect existence, model readability, integrations, finiteness, and wavelength coverage."""
    groups, header, flux_unit, wavelength_unit = spectra_from_x1dints(path)
    spectra = [spectrum for order in sorted(groups) for spectrum in groups[order]]
    integrations = {
        spectrum.integration for spectrum in spectra if spectrum.integration is not None
    }
    actual_integrations = len(integrations) if integrations else max(map(len, groups.values()))
    expected_integrations = _expected_integrations(header)
    finite_flux = sum(int(np.count_nonzero(valid_flux(spectrum))) for spectrum in spectra)
    finite_wavelength = sum(
        int(np.count_nonzero(np.isfinite(spectrum.wavelength))) for spectrum in spectra
    )
    ordered = all(_ordered_wavelength(spectrum) for spectrum in spectra)
    coverage = {
        str(order): _coverage(order_spectra) for order, order_spectra in sorted(groups.items())
    }
    spectral_qa = _spectral_qa_report(groups)
    metadata = {
        "instrument": _header_text(header, "INSTRUME"),
        "exposure_type": _header_text(header, "EXP_TYPE"),
        "detector": _header_text(header, "DETECTOR"),
        "grating": _header_text(header, "GRATING"),
        "filter": _header_text(header, "FILTER"),
        "subarray": _header_text(header, "SUBARRAY"),
    }
    metadata_matches = {
        "instrument": metadata["instrument"] == expected_instrument,
        "exposure_type": metadata["exposure_type"] == expected_exposure_type,
        "detector": expected_detector is None or metadata["detector"] == expected_detector,
    }
    configuration = (metadata["grating"], metadata["filter"])
    return {
        "input": str(path),
        "metadata": metadata,
        "metadata_matches_branch": metadata_matches,
        "checks": {
            "output_exists": path.is_file() and path.stat().st_size > 0,
            "readable_jwst_datamodel": _readable_datamodel(path),
            "positive_integration_count": actual_integrations > 0,
            "integration_count_matches_header": (
                expected_integrations is None or actual_integrations == expected_integrations
            ),
            "finite_extracted_flux": finite_flux > 0,
            "finite_wavelength_solution": finite_wavelength > 0,
            "monotonic_wavelength_per_integration": ordered,
        },
        "integration_count": {
            "extracted": actual_integrations,
            "expected_from_header": expected_integrations,
        },
        "finite_samples": {"flux": finite_flux, "wavelength": finite_wavelength},
        "spectral_qa": spectral_qa,
        "wavelength": {
            "unit": wavelength_unit,
            "coverage_by_order_microns": coverage,
            "detector_gap_configuration": configuration in NIRSPEC_TWO_DETECTOR_CONFIGURATIONS,
        },
        "flux_unit": flux_unit,
    }


def _spectral_qa_report(groups: dict[int, list[Spectrum]]) -> dict[str, Any]:
    per_order: dict[str, Any] = {}
    total = accepted = rejected = 0
    for order, spectra in sorted(groups.items()):
        stack = native_spectral_stack(spectra)
        if stack is None:
            per_order[str(order)] = {
                "native_grid_aligned": False,
                "error": "Integrations do not share one native wavelength grid.",
            }
            continue
        selection = spectral_qa_selection(*stack)
        diagnostics = selection.diagnostics()
        diagnostics["native_grid_aligned"] = True
        per_order[str(order)] = diagnostics
        total += selection.valid_channels.size
        accepted += selection.accepted_count
        rejected += selection.rejected_count
    return {
        "configuration": SPECTRAL_QA_CONFIG.as_provenance(),
        "total_wavelength_bins": int(total),
        "accepted_for_qa": int(accepted),
        "rejected": int(rejected),
        "by_order": per_order,
    }


def write_extracted_spectrum_report(
    path: Path,
    output: Path,
    *,
    expected_instrument: str,
    expected_exposure_type: str,
    expected_detector: str | None,
) -> Path:
    """Write a deterministic JSON QA report without changing pipeline products."""
    report = extracted_spectrum_report(
        path,
        expected_instrument=expected_instrument,
        expected_exposure_type=expected_exposure_type,
        expected_detector=expected_detector,
    )
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _readable_datamodel(path: Path) -> bool:
    try:
        from jwst import datamodels

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = datamodels.open(path)
            model.close()
    except Exception:  # noqa: BLE001 - this is a reported QA check, not pipeline control flow.
        return False
    return True


def _expected_integrations(header: Any) -> int | None:
    start, end = header.get("INTSTART"), header.get("INTEND")
    if isinstance(start, int) and isinstance(end, int) and end >= start:
        return end - start + 1
    value = header.get("NINTS")
    return int(value) if isinstance(value, int) and value > 0 else None


def _ordered_wavelength(spectrum: Spectrum) -> bool:
    wavelength = spectrum.wavelength[np.isfinite(spectrum.wavelength)]
    if wavelength.size < 2:
        return wavelength.size == 1
    difference = np.diff(wavelength)
    return bool(np.all(difference > 0) or np.all(difference < 0))


def _coverage(spectra: list[Spectrum]) -> dict[str, float | None]:
    finite = np.concatenate([item.wavelength[np.isfinite(item.wavelength)] for item in spectra])
    if not finite.size:
        return {"minimum": None, "maximum": None}
    return {"minimum": float(np.min(finite)), "maximum": float(np.max(finite))}


def _header_text(header: Any, key: str) -> str | None:
    value = header.get(key)
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None
