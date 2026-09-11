"""Shared FITS parsing, robust statistics, and plotting utilities for QA."""

from __future__ import annotations

import os
import tempfile
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Pipeline workstations sometimes run with a read-only home directory.  Give
# matplotlib a small writable cache without requiring users to configure it.
_MPL_CACHE = Path(tempfile.gettempdir()) / "jwst-redux-matplotlib"
_MPL_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))
os.environ.setdefault("XDG_CACHE_HOME", str(_MPL_CACHE))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from astropy import units as u
from astropy.io import fits

QA_SCHEMA_VERSION = "6"
PLOT_DPI = 180
MAD_TO_SIGMA = 1.4826
F_LAMBDA_UNIT = u.W / u.m**2 / u.um


@dataclass(frozen=True)
class DynamicSpectrumConfig:
    """Display-only guardrails for SOSS relative-flux maps.

    A wavelength channel is shown only when at least ``finite_fraction_min`` of
    its integrations are finite and its absolute temporal-median flux exceeds
    ``flux_floor_fraction`` of the order's characteristic median flux.  The
    symmetric colour range is the requested percentile of good residuals,
    bounded to prevent a handful of pathological samples from obscuring the
    time-series structure. Channels with extreme own-time-series residuals are
    neutralized before global scaling. These values intentionally affect QA
    provenance.
    """

    finite_fraction_min: float = 0.9
    flux_floor_fraction: float = 0.05
    scale_percentile: float = 99.0
    scale_min_ppt: float = 1.0
    scale_max_ppt: float = 50.0
    pathological_channel_percentile: float = 99.0
    pathological_channel_max_ppt: float = 250.0

    def as_provenance(self) -> dict[str, float]:
        """Return the stable values recorded with generated QA."""
        return {
            "finite_fraction_min": self.finite_fraction_min,
            "flux_floor_fraction": self.flux_floor_fraction,
            "scale_percentile": self.scale_percentile,
            "scale_min_ppt": self.scale_min_ppt,
            "scale_max_ppt": self.scale_max_ppt,
            "pathological_channel_percentile": self.pathological_channel_percentile,
            "pathological_channel_max_ppt": self.pathological_channel_max_ppt,
        }


DYNAMIC_SPECTRUM_CONFIG = DynamicSpectrumConfig()


@dataclass(frozen=True)
class Spectrum:
    """One extracted integration, retaining its native wavelength grid."""

    order: int
    integration: int | None
    time_mjd: float | None
    wavelength: np.ndarray
    flux: np.ndarray
    dq: np.ndarray | None


def qa_subdirectory(root: Path, product: Path) -> Path:
    """Return a collision-safe QA directory derived from the full product stem."""
    path = root / product.name.removesuffix(".fits").removesuffix(".ecsv")
    path.mkdir(parents=True, exist_ok=True)
    return path


def robust_limits(
    values: np.ndarray, *, percentiles: tuple[float, float] = (1, 99)
) -> tuple[float, float]:
    """Finite percentile limits, with a safe non-zero span for images."""
    finite = np.asarray(values, dtype=float)[np.isfinite(values)]
    if not finite.size:
        return (0.0, 1.0)
    low, high = np.percentile(finite, percentiles)
    if not np.isfinite(low) or not np.isfinite(high) or low == high:
        scale = max(abs(float(low)), 1.0)
        return (float(low - 0.01 * scale), float(high + 0.01 * scale))
    return (float(low), float(high))


def finite_median(values: np.ndarray, axis: int | None = None) -> np.ndarray | float:
    """NaN-aware median without emitting all-NaN-slice warnings."""
    data = np.asarray(values, dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmedian(data, axis=axis)


def valid_flux(spectrum: Spectrum) -> np.ndarray:
    """Return finite values excluding non-zero extracted-spectrum DQ samples."""
    valid = np.isfinite(spectrum.wavelength) & np.isfinite(spectrum.flux)
    if spectrum.dq is not None and spectrum.dq.shape == spectrum.flux.shape:
        valid &= spectrum.dq == 0
    return valid


def elapsed_hours(spectra: list[Spectrum]) -> np.ndarray:
    """Elapsed hours from the first usable product time, else integration sequence."""
    times = np.array([np.nan if item.time_mjd is None else item.time_mjd for item in spectra])
    if np.isfinite(times).any():
        return (times - np.nanmin(times)) * 24.0
    return np.arange(len(spectra), dtype=float)


def relative_flux_ppt(flux: np.ndarray) -> np.ndarray:
    """Return per-wavelength deviations from the temporal median in ppt."""
    values = np.asarray(flux, dtype=float)
    median = finite_median(values, axis=0)
    result = np.full_like(values, np.nan)
    valid = np.isfinite(values) & np.isfinite(median)[None, :] & (median[None, :] != 0)
    result[valid] = 1e3 * (values[valid] / np.broadcast_to(median, values.shape)[valid] - 1)
    return result


def dynamic_spectrum_display(
    flux: np.ndarray, config: DynamicSpectrumConfig = DYNAMIC_SPECTRUM_CONFIG
) -> tuple[np.ndarray, np.ndarray, float]:
    """Return display-ready ppt residuals, good channels, and clipped half-range.

    ``flux`` must be shaped ``(integration, wavelength)`` and already have DQ
    samples set to NaN. Bad wavelength channels are returned as NaN so plotting
    can render them with the colormap's neutral bad value.
    """
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2:
        raise ValueError(f"Dynamic spectrum flux must be 2-D, got {values.shape}")
    finite_fraction = np.mean(np.isfinite(values), axis=0)
    median = np.asarray(finite_median(values, axis=0), dtype=float)
    finite_median_flux = np.abs(median[np.isfinite(median)])
    characteristic = float(finite_median(finite_median_flux)) if finite_median_flux.size else np.nan
    floor = config.flux_floor_fraction * characteristic
    good_channels = (
        (finite_fraction >= config.finite_fraction_min)
        & np.isfinite(median)
        & (np.abs(median) >= floor)
    )
    residual = relative_flux_ppt(values)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        channel_amplitude = np.nanpercentile(
            np.abs(residual), config.pathological_channel_percentile, axis=0
        )
    good_channels &= channel_amplitude <= config.pathological_channel_max_ppt
    residual[:, ~good_channels] = np.nan
    finite = np.abs(residual[np.isfinite(residual)])
    raw_limit = float(np.percentile(finite, config.scale_percentile)) if finite.size else 0.0
    limit = float(np.clip(raw_limit, config.scale_min_ppt, config.scale_max_ppt))
    return residual, good_channels, limit


def retained_wavelength_extent(
    wavelength_microns: np.ndarray, good_channels: np.ndarray
) -> tuple[float, float] | None:
    """Return the wavelength span of the dominant contiguous retained run.

    A few isolated retained channels can occur far outside the useful spectral
    region. They do not define the displayed viewport; the longest contiguous
    run does. This changes no validity flag and only crops the presentation.
    """
    wavelength = np.asarray(wavelength_microns, dtype=float)
    good = np.asarray(good_channels, dtype=bool)
    if wavelength.shape != good.shape:
        raise ValueError("Wavelength and good-channel arrays must have identical shapes.")
    indices = np.flatnonzero(good & np.isfinite(wavelength))
    if not indices.size:
        return None
    runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    dominant = max(runs, key=len)
    retained = wavelength[dominant]
    return (float(np.min(retained)), float(np.max(retained)))


def retained_channel_label(good_channels: np.ndarray) -> str:
    """Return a compact retained-channel count annotation."""
    good = np.asarray(good_channels, dtype=bool)
    count = int(np.count_nonzero(good))
    return f"Retained: {count}/{good.size} ({count / good.size:.0%})"


def flux_to_f_lambda(
    flux: np.ndarray, wavelength_microns: np.ndarray, flux_unit: str
) -> np.ndarray:
    """Convert native spectral-density samples to display-only F_lambda values.

    The wavelength grid is already normalized to microns by
    :func:`spectra_from_x1dints`; Astropy's spectral-density equivalency applies
    the wavelength-dependent F_nu <-> F_lambda relation correctly.
    """
    source_unit = u.Unit(flux_unit)
    wavelength = u.Quantity(wavelength_microns, u.um, copy=False)
    values = u.Quantity(flux, source_unit, copy=False)
    return values.to_value(F_LAMBDA_UNIT, equivalencies=u.spectral_density(wavelength))


def product_title(header: fits.Header, suffix: str = "") -> str:
    """Compose a compact identifying title from standard JWST primary metadata."""
    program = str(header.get("PROGRAM", "?"))
    observation = str(header.get("OBSERVTN", "?"))
    visit = str(header.get("VISIT", "?"))
    exposure = str(header.get("EXPOSURE", header.get("EXPOSURE", "?")))
    target = str(header.get("TARGNAME", "unknown target"))
    return f"{target} | Program {program} Obs {observation} Visit {visit} Exp {exposure}{suffix}"


def spectra_from_x1dints(
    path: Path,
) -> tuple[dict[int, list[Spectrum]], fits.Header, str, str]:
    """Read all EXTRACT1D table rows grouped by SOSS order.

    A combined TSO3 product has one EXTRACT1D HDU per segment/order, so every
    table row is deliberately represented as an integration here.
    """
    groups: dict[int, list[Spectrum]] = defaultdict(list)
    with fits.open(path, memmap=True) as hdul:
        header = hdul[0].header.copy()
        flux_unit = ""
        wavelength_unit = ""
        for hdu in hdul:
            if hdu.name.upper() != "EXTRACT1D" or hdu.data is None:
                continue
            names = {name.upper(): name for name in hdu.data.names or ()}
            wave_name, flux_name = names.get("WAVELENGTH"), names.get("FLUX")
            if wave_name is None or flux_name is None:
                continue
            dq_name = names.get("DQ")
            int_name = next(
                (names.get(key) for key in ("INT_NUM", "INTNUM", "INTEGRATION") if names.get(key)),
                None,
            )
            time_name = next(
                (
                    names.get(key)
                    for key in ("TDB-MID", "MJD-AVG", "TDB_MID", "MJD_AVG")
                    if names.get(key)
                ),
                None,
            )
            order_name = next(
                (
                    names.get(key)
                    for key in ("SPORDER", "SPECTRAL_ORDER", "ORDER")
                    if names.get(key)
                ),
                None,
            )
            header_order = hdu.header.get(
                "SPORDER", hdu.header.get("SPECTORD", hdu.header.get("ORDER"))
            )
            flux_unit = flux_unit or (hdu.columns[flux_name].unit or "")
            wavelength_unit = wavelength_unit or (hdu.columns[wave_name].unit or "")
            for row in hdu.data:
                order_value = row[order_name] if order_name else header_order
                try:
                    order = int(np.asarray(order_value).item())
                except (TypeError, ValueError):
                    continue
                integration = _scalar_int(row[int_name]) if int_name else None
                time_value = _scalar_float(row[time_name]) if time_name else None
                groups[order].append(
                    Spectrum(
                        order,
                        integration,
                        time_value,
                        u.Quantity(
                            np.asarray(row[wave_name], dtype=float), wavelength_unit or u.um
                        ).to_value(u.um),
                        np.asarray(row[flux_name], dtype=float),
                        None if dq_name is None else np.asarray(row[dq_name]),
                    )
                )
    for spectra in groups.values():
        if all(item.integration is not None for item in spectra) and len(
            {item.integration for item in spectra}
        ) == len(spectra):
            spectra.sort(key=lambda item: int(item.integration or 0))
    if not groups:
        raise ValueError(f"No usable EXTRACT1D wavelength/flux rows in {path}")
    return dict(groups), header, flux_unit or "Flux", wavelength_unit or "um"


def _scalar_int(value: Any) -> int | None:
    try:
        return int(np.asarray(value).item())
    except (TypeError, ValueError):
        return None


def _scalar_float(value: Any) -> float | None:
    try:
        result = float(np.asarray(value).item())
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def save_figure(figure: plt.Figure, path: Path) -> Path:
    """Save and close an inspection-quality PNG."""
    figure.tight_layout()
    figure.savefig(path, dpi=PLOT_DPI)
    plt.close(figure)
    return path
