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

QA_SCHEMA_VERSION = "11"
PLOT_DPI = 180
MAD_TO_SIGMA = 1.4826
F_LAMBDA_UNIT = u.erg / u.s / u.cm**2 / u.AA
F_LAMBDA_LABEL = "Fλ [erg s⁻¹ cm⁻² Å⁻¹]"


@dataclass(frozen=True)
class SpectroscopicTimeSeriesConfig:
    """Display-only guardrails for SOSS spectroscopic time series.

    A channel is mathematically valid for display when at least
    ``finite_fraction_min`` of its integrations are finite and its absolute
    temporal-median flux exceeds ``flux_floor_fraction`` of the order's
    characteristic median flux. The symmetric colour range is the requested
    percentile of valid residuals, bounded to prevent a handful of extreme
    samples from obscuring time-series structure. Channels with extreme
    own-time-series residuals are separately classified as not science-quality
    but remain visible for diagnostic QA. These values intentionally affect QA
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


SPECTROSCOPIC_TIME_SERIES_CONFIG = SpectroscopicTimeSeriesConfig()


@dataclass(frozen=True)
class SpectralQAConfig:
    """Conservative, instrument-independent channel-selection parameters.

    The two outlier floors keep a narrow but legitimate spectral feature from
    being rejected merely because the surrounding spectrum is very smooth.
    Both tests operate on ratios to robust ensemble levels, never on an
    absolute flux or wavelength cutoff.
    """

    finite_fraction_min: float = 0.9
    robust_sigma: float = 12.0
    representative_flux_ratio_min: float = 1_000.0
    temporal_scatter_ratio_min: float = 30.0
    rejected_example_limit: int = 5

    def as_provenance(self) -> dict[str, float | int]:
        return {
            "finite_fraction_min": self.finite_fraction_min,
            "robust_sigma": self.robust_sigma,
            "representative_flux_ratio_min": self.representative_flux_ratio_min,
            "temporal_scatter_ratio_min": self.temporal_scatter_ratio_min,
            "rejected_example_limit": self.rejected_example_limit,
        }


SPECTRAL_QA_CONFIG = SpectralQAConfig()


@dataclass(frozen=True)
class Spectrum:
    """One extracted integration, retaining its native wavelength grid."""

    order: int
    integration: int | None
    time_mjd: float | None
    wavelength: np.ndarray
    flux: np.ndarray
    dq: np.ndarray | None


@dataclass(frozen=True)
class SpectralQASelection:
    """A native-grid flux stack and its reusable QA-valid channel mask."""

    wavelength: np.ndarray
    flux: np.ndarray
    valid_channels: np.ndarray
    finite_fraction: np.ndarray
    representative_flux: np.ndarray
    temporal_scatter_ppt: np.ndarray
    representative_flux_ratio: np.ndarray
    temporal_scatter_ratio: np.ndarray
    rejection_reasons: tuple[tuple[str, ...], ...]

    @property
    def accepted_count(self) -> int:
        return int(np.count_nonzero(self.valid_channels))

    @property
    def rejected_count(self) -> int:
        return int(self.valid_channels.size - self.accepted_count)

    def masked_flux(self) -> np.ndarray:
        """Return a copy with rejected wavelength channels represented as NaN."""
        result = self.flux.copy()
        result[:, ~self.valid_channels] = np.nan
        return result

    def diagnostics(self, config: SpectralQAConfig = SPECTRAL_QA_CONFIG) -> dict[str, Any]:
        """Return compact counts and the most extreme rejected channels."""
        severity = np.fmax(
            np.nan_to_num(
                self.representative_flux_ratio / config.representative_flux_ratio_min,
                nan=0.0,
                posinf=np.inf,
            ),
            np.nan_to_num(
                self.temporal_scatter_ratio / config.temporal_scatter_ratio_min,
                nan=0.0,
                posinf=np.inf,
            ),
        )
        rejected = np.flatnonzero(~self.valid_channels)
        ranked = rejected[np.argsort(severity[rejected])[::-1]]
        examples = []
        for index in ranked[: config.rejected_example_limit]:
            examples.append(
                {
                    "index": int(index),
                    "wavelength_microns": _finite_float(self.wavelength[index]),
                    "finite_fraction": float(self.finite_fraction[index]),
                    "representative_flux_native": _finite_float(self.representative_flux[index]),
                    "representative_flux_ratio": _finite_float(
                        self.representative_flux_ratio[index]
                    ),
                    "temporal_scatter_ppt": _finite_float(self.temporal_scatter_ppt[index]),
                    "temporal_scatter_ratio": _finite_float(self.temporal_scatter_ratio[index]),
                    "reasons": list(self.rejection_reasons[index]),
                }
            )
        reason_counts: dict[str, int] = defaultdict(int)
        for reasons in self.rejection_reasons:
            for reason in reasons:
                reason_counts[reason] += 1
        return {
            "total_wavelength_bins": int(self.valid_channels.size),
            "accepted_for_qa": self.accepted_count,
            "rejected": self.rejected_count,
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "most_extreme_rejected_bins": examples,
        }


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


def native_spectral_stack(
    spectra: list[Spectrum],
) -> tuple[np.ndarray, np.ndarray] | None:
    """Stack integrations on their shared native grid with unusable samples masked."""
    if not spectra:
        return None
    reference = spectra[0].wavelength
    if not all(
        item.wavelength.shape == reference.shape
        and np.allclose(item.wavelength, reference, equal_nan=True, rtol=1e-7, atol=0)
        for item in spectra
    ):
        return None
    flux = np.vstack([np.where(valid_flux(item), item.flux, np.nan) for item in spectra])
    return reference.copy(), flux


def spectral_qa_selection(
    wavelength: np.ndarray,
    flux: np.ndarray,
    config: SpectralQAConfig = SPECTRAL_QA_CONFIG,
) -> SpectralQASelection:
    """Select channels suitable for absolute-spectrum and common-mode QA.

    Inputs are a one-dimensional wavelength grid in microns and a flux array
    shaped ``(integration, wavelength)``. DQ-unusable samples must already be
    NaN, as produced by :func:`native_spectral_stack`. The mask first requires
    finite wavelength, adequate finite sample coverage, and a finite non-zero
    temporal median. It then rejects only extreme upper outliers relative to
    robust ensemble distributions of representative flux and temporal scatter.
    """
    wave = np.asarray(wavelength, dtype=float)
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2 or wave.ndim != 1 or values.shape[1] != wave.size:
        raise ValueError(
            "Spectral QA requires wavelength shape (channel,) and flux shape "
            f"(integration, channel); got {wave.shape} and {values.shape}."
        )

    finite_fraction = np.mean(np.isfinite(values), axis=0)
    representative = np.asarray(finite_median(values, axis=0), dtype=float)
    scatter = relative_scatter_ppt(values)
    base_valid = (
        np.isfinite(wave)
        & (finite_fraction >= config.finite_fraction_min)
        & np.isfinite(representative)
        & (representative != 0)
    )

    amplitude_ratio, amplitude_outlier = _robust_upper_ratio_outliers(
        np.abs(representative),
        base_valid,
        robust_sigma=config.robust_sigma,
        minimum_ratio=config.representative_flux_ratio_min,
        offset=0.0,
    )
    scatter_ratio, scatter_outlier = _robust_upper_ratio_outliers(
        scatter,
        base_valid,
        robust_sigma=config.robust_sigma,
        minimum_ratio=config.temporal_scatter_ratio_min,
        offset=1.0,
    )
    valid = base_valid & ~amplitude_outlier & ~scatter_outlier

    reasons: list[tuple[str, ...]] = []
    for index in range(wave.size):
        channel_reasons = []
        if not np.isfinite(wave[index]):
            channel_reasons.append("nonfinite_wavelength")
        if finite_fraction[index] < config.finite_fraction_min:
            channel_reasons.append("insufficient_finite_or_dq_usable_flux")
        if not np.isfinite(representative[index]):
            channel_reasons.append("nonfinite_representative_flux")
        elif representative[index] == 0:
            channel_reasons.append("zero_representative_flux")
        if amplitude_outlier[index]:
            channel_reasons.append("representative_flux_outlier")
        if scatter_outlier[index]:
            channel_reasons.append("temporal_variability_outlier")
        reasons.append(tuple(channel_reasons))

    return SpectralQASelection(
        wavelength=wave,
        flux=values,
        valid_channels=valid,
        finite_fraction=finite_fraction,
        representative_flux=representative,
        temporal_scatter_ppt=scatter,
        representative_flux_ratio=amplitude_ratio,
        temporal_scatter_ratio=scatter_ratio,
        rejection_reasons=tuple(reasons),
    )


def qa_common_mode_ppt(selection: SpectralQASelection) -> np.ndarray:
    """Combine channel-normalized valid fluxes into a robust QA common mode."""
    valid = selection.valid_channels
    if not np.any(valid):
        raise ValueError("No wavelength channels passed the spectral QA mask.")
    flux = selection.flux[:, valid]
    representative = selection.representative_flux[valid]
    relative = flux / representative[None, :]
    return 1e3 * (np.asarray(finite_median(relative, axis=1), dtype=float) - 1.0)


def _robust_upper_ratio_outliers(
    values: np.ndarray,
    candidate: np.ndarray,
    *,
    robust_sigma: float,
    minimum_ratio: float,
    offset: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Flag conservative upper outliers in a positive ensemble distribution."""
    metric = np.asarray(values, dtype=float)
    usable = np.asarray(candidate, dtype=bool) & np.isfinite(metric) & (metric >= 0)
    ratio = np.full(metric.shape, np.nan, dtype=float)
    outlier = np.zeros(metric.shape, dtype=bool)
    if not np.any(usable):
        return ratio, outlier

    transformed = np.full(metric.shape, np.nan, dtype=float)
    transformed[usable] = np.log10(metric[usable] + offset)
    center = float(finite_median(transformed[usable]))
    spread = MAD_TO_SIGMA * float(finite_median(np.abs(transformed[usable] - center)))
    threshold_delta = max(robust_sigma * spread, np.log10(minimum_ratio))
    outlier[usable] = transformed[usable] - center > threshold_delta
    ratio[usable] = 10.0 ** (transformed[usable] - center)
    return ratio, outlier


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


def spectroscopic_time_series_validity_mask(
    flux: np.ndarray,
    config: SpectroscopicTimeSeriesConfig = SPECTROSCOPIC_TIME_SERIES_CONFIG,
) -> np.ndarray:
    """Return per-channel mathematical validity for SOSS QA quantities.

    ``flux`` is shaped ``(integration, wavelength)`` with DQ-invalid samples
    already represented as NaN. Invalid channels lack adequate finite coverage
    or a stable, finite temporal-median normalization denominator. This mask is
    shared by every wavelength-resolved QA diagnostic, where invalid samples
    render neutrally and do not influence display limits.
    """
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2:
        raise ValueError(f"Spectroscopic time-series flux must be 2-D, got {values.shape}")
    finite_fraction = np.mean(np.isfinite(values), axis=0)
    median = np.asarray(finite_median(values, axis=0), dtype=float)
    finite_median_flux = np.abs(median[np.isfinite(median)])
    characteristic = float(finite_median(finite_median_flux)) if finite_median_flux.size else np.nan
    floor = config.flux_floor_fraction * characteristic
    return (
        (finite_fraction >= config.finite_fraction_min)
        & np.isfinite(median)
        & (np.abs(median) > floor)
    )


def spectroscopic_time_series_science_quality_mask(
    flux: np.ndarray,
    config: SpectroscopicTimeSeriesConfig = SPECTROSCOPIC_TIME_SERIES_CONFIG,
) -> np.ndarray:
    """Classify channels that also pass the stricter variability criterion.

    This classification preserves the former pathological-variation rejection,
    but it is metadata for interpretation rather than a default display mask.
    Noisy, mathematically valid channels therefore remain visible in QA.
    """
    values = np.asarray(flux, dtype=float)
    valid_channels = spectroscopic_time_series_validity_mask(values, config)
    residual = relative_flux_ppt(values)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        channel_amplitude = np.nanpercentile(
            np.abs(residual), config.pathological_channel_percentile, axis=0
        )
    return valid_channels & (channel_amplitude <= config.pathological_channel_max_ppt)


def robust_ppt_limit(
    values: np.ndarray,
    config: SpectroscopicTimeSeriesConfig = SPECTROSCOPIC_TIME_SERIES_CONFIG,
) -> float:
    """Return a symmetric, bounded colour range from finite ppt samples."""
    finite = np.abs(np.asarray(values, dtype=float)[np.isfinite(values)])
    raw_limit = float(np.percentile(finite, config.scale_percentile)) if finite.size else 0.0
    return float(np.clip(raw_limit, config.scale_min_ppt, config.scale_max_ppt))


def spectroscopic_time_series_display(
    flux: np.ndarray,
    config: SpectroscopicTimeSeriesConfig = SPECTROSCOPIC_TIME_SERIES_CONFIG,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return residuals, validity/science masks, and clipped display half-range.

    ``flux`` must be shaped ``(integration, wavelength)`` and already have DQ
    samples set to NaN. Only mathematically invalid wavelength channels are
    returned as NaN so plotting can render them with the colormap's neutral bad
    value. Science-quality failures intentionally remain visible.
    """
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2:
        raise ValueError(f"Spectroscopic time-series flux must be 2-D, got {values.shape}")
    valid_channels = spectroscopic_time_series_validity_mask(values, config)
    science_quality_channels = spectroscopic_time_series_science_quality_mask(values, config)
    residual = relative_flux_ppt(values)
    residual[:, ~valid_channels] = np.nan
    return residual, valid_channels, science_quality_channels, robust_ppt_limit(residual, config)


def relative_scatter_ppt(
    flux: np.ndarray,
    valid_channels: np.ndarray | None = None,
) -> np.ndarray:
    """Return 1.4826 MAD temporal scatter divided by median flux, in ppt."""
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2:
        raise ValueError(f"Scatter flux must be 2-D, got {values.shape}")
    median = np.asarray(finite_median(values, axis=0), dtype=float)
    mad = np.asarray(finite_median(np.abs(values - median[None, :]), axis=0), dtype=float)
    result = np.full(median.shape, np.nan, dtype=float)
    valid = np.isfinite(median) & (median != 0) & np.isfinite(mad)
    result[valid] = 1e3 * MAD_TO_SIGMA * mad[valid] / np.abs(median[valid])
    if valid_channels is not None:
        valid = np.asarray(valid_channels, dtype=bool)
        if valid.shape != result.shape:
            raise ValueError("Scatter validity mask must match the wavelength dimension.")
        result[~valid] = np.nan
    return result


def point_to_point_difference_ppt(
    flux: np.ndarray,
    valid_channels: np.ndarray | None = None,
) -> np.ndarray:
    """Return later-minus-earlier consecutive-integration differences in ppt.

    Row ``i`` is the difference between integrations ``i + 1`` and ``i`` and
    is plotted at the timestamp of the later integration. Consequently the
    output has one fewer time row than the input.
    """
    values = np.asarray(flux, dtype=float)
    if values.ndim != 2:
        raise ValueError(f"Point-to-point flux must be 2-D, got {values.shape}")
    median = np.asarray(finite_median(values, axis=0), dtype=float)
    result = np.full((max(values.shape[0] - 1, 0), values.shape[1]), np.nan, dtype=float)
    if values.shape[0] < 2:
        return result
    denominator = np.broadcast_to(median, result.shape)
    valid = (
        np.isfinite(values[1:])
        & np.isfinite(values[:-1])
        & np.isfinite(denominator)
        & (denominator != 0)
    )
    result[valid] = 1e3 * (values[1:][valid] - values[:-1][valid]) / denominator[valid]
    if valid_channels is not None:
        valid_channels = np.asarray(valid_channels, dtype=bool)
        if valid_channels.shape != median.shape:
            raise ValueError("Point-to-point validity mask must match the wavelength dimension.")
        result[:, ~valid_channels] = np.nan
    return result


def channel_classification_label(
    valid_channels: np.ndarray, science_quality_channels: np.ndarray
) -> str:
    """Return validity and stricter science-quality counts in one annotation."""
    valid = np.asarray(valid_channels, dtype=bool)
    science_quality = np.asarray(science_quality_channels, dtype=bool)
    if valid.shape != science_quality.shape:
        raise ValueError(
            "Validity and science-quality masks must have matching wavelength dimensions."
        )
    science_quality = science_quality & valid
    valid_count = int(np.count_nonzero(valid))
    science_quality_count = int(np.count_nonzero(science_quality))
    total = valid.size
    fraction = science_quality_count / total if total else 0.0
    return (
        f"Valid channels shown: {valid_count}/{total}\n"
        f"Science-quality channels: {science_quality_count}/{total} ({fraction:.0%})"
    )


def flux_to_f_lambda(
    flux: np.ndarray, wavelength_microns: np.ndarray, flux_unit: str
) -> np.ndarray:
    """Convert native spectral-density samples to cgs F_lambda values.

    The wavelength grid is already normalized to microns by
    :func:`spectra_from_x1dints`; Astropy's spectral-density equivalency applies
    the wavelength-dependent F_nu <-> F_lambda relation correctly.
    """
    try:
        source_unit = u.Unit(flux_unit)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Unknown extracted-spectrum flux unit {flux_unit!r}.") from error
    wavelength = u.Quantity(wavelength_microns, u.um, copy=False)
    values = u.Quantity(flux, source_unit, copy=False)
    try:
        return values.to_value(F_LAMBDA_UNIT, equivalencies=u.spectral_density(wavelength))
    except u.UnitConversionError as error:
        raise ValueError(
            f"Extracted-spectrum flux unit {flux_unit!r} is not a supported spectral "
            f"flux-density unit convertible to {F_LAMBDA_UNIT}."
        ) from error


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
    """Read all EXTRACT1D table rows grouped by spectral order.

    A combined TSO3 product has one EXTRACT1D HDU per segment/order, so every
    table row is deliberately represented as an integration here. NIRSpec/BOTS
    products without an explicit order keyword are their single spectral order.
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
            if header_order is None and header.get("EXP_TYPE") == "NRS_BRIGHTOBJ":
                header_order = 1
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


def _finite_float(value: Any) -> float | None:
    """Return a JSON-safe finite float or ``None``."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def save_figure(figure: plt.Figure, path: Path) -> Path:
    """Save and close an inspection-quality PNG."""
    figure.tight_layout()
    figure.savefig(path, dpi=PLOT_DPI)
    plt.close(figure)
    return path
