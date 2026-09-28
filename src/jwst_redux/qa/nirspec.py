"""Detector-aware NIRSpec/BOTS extracted-spectrum QA."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from astropy.table import Table
from matplotlib import colors

from .common import (
    F_LAMBDA_LABEL,
    SpectralQASelection,
    Spectrum,
    center_finite_median,
    elapsed_hours,
    filter_white_light_curve,
    finite_median,
    flux_to_f_lambda,
    native_spectral_stack,
    point_to_point_difference_ppt,
    product_title,
    qa_common_mode_ppt,
    relative_scatter_ppt,
    robust_limits,
    robust_ppt_limit,
    save_figure,
    spectra_from_x1dints,
    spectral_qa_selection,
    spectroscopic_time_series_display,
    white_light_quicklook_plot,
    write_filtered_white_light,
)


@dataclass(frozen=True)
class OfficialWhiteLight:
    """One detector-specific official JWST WhiteLightStep curve."""

    elapsed_hours: np.ndarray
    flux: np.ndarray
    flux_unit: str
    time_column: str
    flux_column: str


def generate(
    path: Path,
    output_dir: Path,
    *,
    stage: str,
    white_light_path: Path | None = None,
    quicklook_cadence_minutes: float = 2.0,
) -> tuple[Path, ...]:
    """Create BOTS diagnostics on the native detector wavelength grid."""
    groups, header, flux_unit, _ = spectra_from_x1dints(path)
    spectra = [item for order in sorted(groups) for item in groups[order]]
    detector = str(header.get("DETECTOR", "unknown detector")).upper()
    grating = str(header.get("GRATING", "?"))
    filter_name = str(header.get("FILTER", "?"))
    title = product_title(header, f" | {stage} NIRSpec/BOTS {detector} {grating}/{filter_name}")
    stack = _masked_stack(spectra)
    if stack is None:
        raise ValueError("NIRSpec/BOTS QA requires integrations on one shared native grid.")
    selection = spectral_qa_selection(*stack)
    qa_white_light = qa_white_light_ppt(selection)
    native_time = np.array(
        [np.nan if item.time_mjd is None else item.time_mjd for item in spectra], dtype=float
    )
    filtered_white_light = filter_white_light_curve(native_time, qa_white_light)
    return (
        _spectra_plot(spectra, selection, title, flux_unit, output_dir / "spectra.png"),
        _white_light_plot(
            spectra,
            selection,
            qa_white_light,
            title,
            output_dir / "white_light.png",
            detector=detector,
            official=white_light_path,
        ),
        white_light_quicklook_plot(
            [filtered_white_light],
            title,
            output_dir / "white_light_quicklook.png",
            cadence_minutes=quicklook_cadence_minutes,
        ),
        write_filtered_white_light(
            [filtered_white_light],
            output_dir / "white_light_filtered.ecsv",
            cadence_minutes=quicklook_cadence_minutes,
            source_mode="NIRSpec/BOTS",
            source_stage=stage,
        ),
        _time_series_plot(selection, spectra, title, output_dir / "spectroscopic_time_series.png"),
        _scatter_plot(selection, title, output_dir / "scatter_spectrum.png"),
        _difference_plot(selection, spectra, title, output_dir / "point_to_point_difference.png"),
    )


def qa_white_light_ppt(selection: SpectralQASelection) -> np.ndarray:
    """Return the same median-zeroed robust common mode used by all BOTS QA."""
    return center_finite_median(qa_common_mode_ppt(selection))


def _masked_stack(spectra: list[Spectrum]) -> tuple[np.ndarray, np.ndarray] | None:
    return native_spectral_stack(spectra)


def _spectra_plot(
    spectra: list[Spectrum],
    selection: SpectralQASelection,
    title: str,
    flux_unit: str,
    output: Path,
) -> Path:
    fig, axis = plt.subplots(figsize=(11, 4.5))
    converted = flux_to_f_lambda(selection.flux, selection.wavelength[None, :], flux_unit)
    converted[:, ~selection.valid_channels] = np.nan
    for index, row in enumerate(converted):
        axis.plot(
            selection.wavelength,
            row,
            color="C0",
            alpha=min(0.25, 8 / len(spectra)),
            linewidth=0.5,
            label="Individual integrations" if index == 0 else None,
        )
    median = np.asarray(finite_median(converted, axis=0), dtype=float)
    axis.plot(
        selection.wavelength,
        median,
        color="C1",
        linewidth=1.5,
        label="Temporal median",
    )
    accepted_values = converted[:, selection.valid_channels]
    low, high = robust_limits(accepted_values, percentiles=(0.5, 99.5))
    margin = 0.05 * (high - low)
    axis.set_ylim(low - margin, high + margin)
    rejected = ~selection.valid_channels & np.isfinite(selection.wavelength)
    if np.any(rejected):
        axis.scatter(
            selection.wavelength[rejected],
            np.full(np.count_nonzero(rejected), low - 0.5 * margin),
            marker="|",
            color="0.35",
            s=24,
            label="Rejected QA channels",
        )
    axis.text(
        0.99,
        0.03,
        _selection_label(selection),
        transform=axis.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 2},
    )
    axis.set(title=title, xlabel="Wavelength [µm]", ylabel=F_LAMBDA_LABEL)
    axis.legend(loc="best")
    return save_figure(fig, output)


def _white_light_plot(
    spectra: list[Spectrum],
    selection: SpectralQASelection,
    qa_common_mode: np.ndarray,
    title: str,
    output: Path,
    *,
    detector: str,
    official: Path | None,
) -> Path:
    official_curve = read_official_white_light(official, detector) if official is not None else None
    rows = 2 if official_curve is not None else 1
    fig, axes = plt.subplots(rows, 1, figsize=(10, 4.5 * rows), squeeze=False)
    proxy_axis = axes[0, 0]
    proxy_axis.plot(elapsed_hours(spectra), qa_common_mode, marker=".", linewidth=1)
    proxy_axis.axhline(0, color="0.4", linewidth=1)
    proxy_axis.set(
        title=title,
        xlabel="Elapsed time [hours]" if official_curve is None else None,
        ylabel="Robust QA common mode [ppt]",
    )
    proxy_axis.text(
        0.01,
        0.03,
        "Median across channel-normalized QA-valid flux; diagnostic only.\n"
        + _selection_label(selection),
        transform=proxy_axis.transAxes,
        fontsize=8,
        va="bottom",
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 2},
    )
    if official_curve is not None:
        official_axis = axes[1, 0]
        baseline = finite_median(official_curve.flux)
        if not np.isfinite(baseline) or baseline == 0:
            raise ValueError("Official JWST white-light flux has no finite non-zero median.")
        official_ppt = 1e3 * (official_curve.flux / baseline - 1.0)
        official_axis.plot(
            official_curve.elapsed_hours,
            official_ppt,
            marker=".",
            linewidth=1,
            color="C3",
        )
        official_axis.axhline(0, color="0.4", linewidth=1)
        official_axis.set(
            title=(
                "Official JWST WhiteLightStep wavelength sum "
                f"({official_curve.flux_column}; independent scale)"
            ),
            xlabel="Elapsed time [hours]",
            ylabel="Official median-normalized flux [ppt]",
        )
    return save_figure(fig, output)


def _time_series_plot(
    selection: SpectralQASelection,
    spectra: list[Spectrum],
    title: str,
    output: Path,
) -> Path:
    fig, axis = plt.subplots(figsize=(11, 4.5))
    residual, _, _, limit = spectroscopic_time_series_display(selection.masked_flux())
    image = axis.pcolormesh(
        elapsed_hours(spectra),
        selection.wavelength,
        residual.T,
        shading="auto",
        cmap=plt.get_cmap("RdBu_r").with_extremes(bad="0.82"),
        norm=colors.TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit),
    )
    fig.colorbar(image, ax=axis, label="Relative flux [ppt]")
    axis.set(title=title, xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
    return save_figure(fig, output)


def _scatter_plot(selection: SpectralQASelection, title: str, output: Path) -> Path:
    fig, axis = plt.subplots(figsize=(11, 4.5))
    scatter = relative_scatter_ppt(selection.masked_flux(), selection.valid_channels)
    axis.plot(selection.wavelength, scatter, color="C3", linewidth=0.8)
    finite = scatter[np.isfinite(scatter)]
    if finite.size:
        _, upper = robust_limits(finite)
        axis.set_ylim(0, max(upper, 1.0))
    axis.set(title=title, xlabel="Wavelength [µm]", ylabel="Relative scatter [ppt]")
    return save_figure(fig, output)


def _difference_plot(
    selection: SpectralQASelection,
    spectra: list[Spectrum],
    title: str,
    output: Path,
) -> Path:
    fig, axis = plt.subplots(figsize=(11, 4.5))
    difference = point_to_point_difference_ppt(selection.masked_flux(), selection.valid_channels)
    if difference.shape[0]:
        limit = robust_ppt_limit(difference)
        image = axis.pcolormesh(
            elapsed_hours(spectra)[1:],
            selection.wavelength,
            difference.T,
            shading="auto",
            cmap=plt.get_cmap("RdBu_r").with_extremes(bad="0.82"),
            norm=colors.TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit),
        )
        fig.colorbar(image, ax=axis, label="Point-to-point relative difference [ppt]")
    else:
        axis.text(0.5, 0.5, "At least two integrations are required.", ha="center")
    axis.set(title=title, xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
    return save_figure(fig, output)


def read_official_white_light(path: Path, detector: str) -> OfficialWhiteLight | None:
    """Read detector-suffixed NIRSpec WhiteLightStep columns when available."""
    table = Table.read(path, format="ascii.ecsv")
    names = {name.lower(): name for name in table.colnames}
    suffix = detector.lower()
    time_name = (
        names.get(f"bjd_tdb_{suffix}")
        or names.get(f"mjd_utc_{suffix}")
        or names.get("bjd_tdb")
        or names.get("mjd_utc")
        or next(
            (
                name
                for lower, name in names.items()
                if lower.startswith(("bjd_tdb_", "mjd_utc_")) and lower.endswith(f"_{suffix}")
            ),
            None,
        )
    )
    flux_name = (
        names.get(f"whitelight_flux_{suffix}")
        or names.get("whitelight_flux")
        or next(
            (
                name
                for lower, name in names.items()
                if lower.startswith("whitelight_flux_") and lower.endswith(f"_{suffix}")
            ),
            None,
        )
    )
    if time_name is None or flux_name is None:
        return None
    time = np.asarray(table[time_name], dtype=float)
    flux = np.asarray(table[flux_name], dtype=float)
    return OfficialWhiteLight(
        elapsed_hours=(time - np.nanmin(time)) * 24.0,
        flux=flux,
        flux_unit=str(table[flux_name].unit or ""),
        time_column=time_name,
        flux_column=flux_name,
    )


def _selection_label(selection: SpectralQASelection) -> str:
    return (
        f"Spectral QA: {selection.accepted_count}/{selection.valid_channels.size} "
        f"channels accepted; {selection.rejected_count} rejected"
    )
