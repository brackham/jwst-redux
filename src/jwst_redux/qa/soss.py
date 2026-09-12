"""SOSS extracted-spectrum plotting shared by Stage 2 and Stage 3 QA."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import colors

from .common import (
    Spectrum,
    elapsed_hours,
    finite_median,
    flux_to_f_lambda,
    point_to_point_difference_ppt,
    product_title,
    relative_scatter_ppt,
    retained_channel_label,
    robust_limits,
    robust_ppt_limit,
    save_figure,
    spectroscopic_time_series_display,
    spectroscopic_time_series_quality_mask,
    valid_flux,
)

SPECTROSCOPIC_TIME_SERIES_COLORBAR_LABEL = "Relative flux [ppt]"
POINT_TO_POINT_COLORBAR_LABEL = "Point-to-point relative difference [ppt]"
SOSS_ORDER_COLORS = {1: "C0", 2: "C1", 3: "C2"}


def spectroscopic_time_series_colormap():
    """Return a zero-centered diverging map with neutral masked-channel colour."""
    return plt.get_cmap("RdBu_r").with_extremes(bad="0.82")


def _masked_stack(spectra: list[Spectrum]) -> tuple[np.ndarray, np.ndarray] | None:
    """Make a native-grid stack only when every integration has the same grid."""
    reference = spectra[0].wavelength
    if not all(
        item.wavelength.shape == reference.shape
        and np.allclose(item.wavelength, reference, equal_nan=True, rtol=1e-7, atol=0)
        for item in spectra
    ):
        return None
    flux = np.vstack([np.where(valid_flux(item), item.flux, np.nan) for item in spectra])
    return reference, flux


def spectroscopic_time_series_window(
    order: int, wavelength_windows: dict[int, tuple[float, float]]
) -> tuple[float, float]:
    """Return the configured display window for one NIRISS/SOSS order."""
    try:
        return wavelength_windows[order]
    except KeyError as error:
        raise ValueError(f"No spectroscopic time-series wavelength window for SOSS order {order}.") from error


def select_spectroscopic_time_series_window(
    wavelength: np.ndarray, flux: np.ndarray, window: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """Select channels in the configured display window before quality masking."""
    lower, upper = window
    in_window = np.isfinite(wavelength) & (wavelength >= lower) & (wavelength <= upper)
    return wavelength[in_window], flux[:, in_window]


def spectra_plot(
    groups: dict[int, list[Spectrum]],
    title: str,
    flux_unit: str,
    output: Path,
    wavelength_windows: dict[int, tuple[float, float]],
) -> Path:
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.2 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
        for number, item in enumerate(spectra):
            valid = valid_flux(item)
            display_flux = flux_to_f_lambda(item.flux, item.wavelength, flux_unit)
            axis.plot(
                item.wavelength[valid],
                display_flux[valid],
                color="C0",
                alpha=min(0.25, 8 / max(len(spectra), 1)),
                linewidth=0.5,
                label="Individual integrations" if number == 0 else None,
            )
        stack = _masked_stack(spectra)
        if stack is not None:
            wavelength, flux = stack
            median = flux_to_f_lambda(finite_median(flux, axis=0), wavelength, flux_unit)
            valid = np.isfinite(wavelength) & np.isfinite(median)
            axis.plot(
                wavelength[valid], median[valid], color="C1", linewidth=1.5, label="Temporal median"
            )
        for boundary in spectroscopic_time_series_window(order, wavelength_windows):
            axis.axvline(boundary, color="0.35", linestyle=":", linewidth=0.8, alpha=0.7)
        axis.set(
            title=f"Order {order}",
            xlabel="Wavelength [µm]",
            ylabel="Fλ [W m⁻² µm⁻¹]",
        )
        axis.legend(loc="upper right")
    fig.suptitle(title)
    return save_figure(fig, output)


def white_light_proxy_plot(
    groups: dict[int, list[Spectrum]],
    title: str,
    output: Path,
    *,
    official: dict[int, tuple[np.ndarray, np.ndarray]] | None = None,
) -> Path:
    """Plot combined and per-order normalized SOSS white-light curves.

    The first panel overlays every available order.  The following three
    panels always reserve one row for Orders 1, 2, and 3, respectively, so
    their colors and positions remain comparable even when an extracted
    product does not contain every order.
    """
    fig, axes = plt.subplots(4, 1, figsize=(10, 12.8), sharex=True, squeeze=False)
    combined, *order_axes = axes[:, 0]
    if official is not None:
        curves = {
            order: (times, flux / finite_median(flux)) for order, (times, flux) in official.items()
        }
        ylabel = "Normalized official TSO3 white-light flux"
    else:
        curves = {}
        for order, spectra in groups.items():
            values = np.array(
                [np.nansum(np.where(valid_flux(item), item.flux, np.nan)) for item in spectra]
            )
            baseline = finite_median(values)
            curves[order] = (elapsed_hours(spectra), values / baseline)
        ylabel = "Normalized QA-derived white-light proxy"

    for order, (times, normalized_flux) in sorted(curves.items()):
        combined.plot(
            times,
            normalized_flux,
            marker=".",
            linewidth=1,
            color=SOSS_ORDER_COLORS.get(order, f"C{order - 1}"),
            label=f"Order {order}",
        )
    combined.set(title=title, ylabel=ylabel)
    combined.legend(loc="best")

    for order, axis in enumerate(order_axes, start=1):
        curve = curves.get(order)
        if curve is None:
            axis.text(
                0.5,
                0.5,
                f"No Order {order} white-light data.",
                ha="center",
                va="center",
                transform=axis.transAxes,
            )
        else:
            times, normalized_flux = curve
            axis.plot(
                times,
                normalized_flux,
                marker=".",
                linewidth=1,
                color=SOSS_ORDER_COLORS[order],
            )
        axis.set(title=f"Order {order}", ylabel=ylabel)

    for axis in axes[:, 0]:
        axis.axhline(1, color="0.4", linewidth=1)
    if official is None:
        combined.text(
            0.01,
            0.02,
            "QA-derived: sum of unflagged finite extracted flux samples.",
            transform=combined.transAxes,
            fontsize=8,
            va="bottom",
        )
    order_axes[-1].set_xlabel("Elapsed time [hours]")
    return save_figure(fig, output)


def plot_spectroscopic_time_series(
    groups: dict[int, list[Spectrum]],
    title: str,
    output: Path,
    wavelength_windows: dict[int, tuple[float, float]],
) -> Path:
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.4 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
        window = spectroscopic_time_series_window(order, wavelength_windows)
        stack = _masked_stack(spectra)
        if stack is None:
            axis.text(
                0.5,
                0.5,
                "Native wavelength grids differ; no interpolation applied.",
                ha="center",
                va="center",
                transform=axis.transAxes,
            )
            axis.set(
                title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]"
            )
            axis.set_ylim(*window)
            continue
        wavelength, flux = stack
        wavelength, flux = select_spectroscopic_time_series_window(wavelength, flux, window)
        if not wavelength.size:
            axis.text(
                0.5,
                0.5,
                "No extracted channels in configured wavelength window.",
                ha="center",
                va="center",
                transform=axis.transAxes,
            )
            axis.set(
                title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]"
            )
            axis.set_ylim(*window)
            continue
        residual, good_channels, limit = spectroscopic_time_series_display(flux)
        cmap = spectroscopic_time_series_colormap()
        image = axis.pcolormesh(
            elapsed_hours(spectra),
            wavelength,
            residual.T,
            shading="auto",
            cmap=cmap,
            norm=colors.TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit),
        )
        axis.set(title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
        axis.set_ylim(*window)
        axis.text(
            0.99,
            0.97,
            retained_channel_label(good_channels),
            transform=axis.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 2},
        )
        fig.colorbar(image, ax=axis, label=SPECTROSCOPIC_TIME_SERIES_COLORBAR_LABEL)
    fig.suptitle(title)
    return save_figure(fig, output)


def plot_scatter_spectrum(
    groups: dict[int, list[Spectrum]],
    title: str,
    output: Path,
    wavelength_windows: dict[int, tuple[float, float]],
) -> Path:
    """Plot robust per-wavelength temporal scatter in the configured windows."""
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.2 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
        window = spectroscopic_time_series_window(order, wavelength_windows)
        stack = _masked_stack(spectra)
        if stack is None:
            _no_native_grid(axis, order, window)
            continue
        wavelength, flux = select_spectroscopic_time_series_window(*stack, window)
        if not wavelength.size:
            _no_window_data(axis, order, window)
            continue
        good_channels = spectroscopic_time_series_quality_mask(flux)
        scatter = relative_scatter_ppt(flux, good_channels)
        axis.plot(wavelength, scatter, color="C3", linewidth=0.8)
        finite = scatter[np.isfinite(scatter)]
        if finite.size:
            _, upper = robust_limits(finite)
            axis.set_ylim(bottom=0, top=max(upper, 1.0))
        axis.set(
            title=f"Order {order}",
            xlabel="Wavelength [µm]",
            ylabel="Relative scatter [ppt]",
            xlim=window,
        )
        axis.text(
            0.99,
            0.97,
            retained_channel_label(good_channels),
            transform=axis.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 2},
        )
    fig.suptitle(title)
    return save_figure(fig, output)


def plot_point_to_point_difference(
    groups: dict[int, list[Spectrum]],
    title: str,
    output: Path,
    wavelength_windows: dict[int, tuple[float, float]],
) -> Path:
    """Plot consecutive-integration relative differences at later timestamps."""
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.4 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
        window = spectroscopic_time_series_window(order, wavelength_windows)
        stack = _masked_stack(spectra)
        if stack is None:
            _no_native_grid(axis, order, window)
            continue
        wavelength, flux = select_spectroscopic_time_series_window(*stack, window)
        if not wavelength.size:
            _no_window_data(axis, order, window)
            continue
        good_channels = spectroscopic_time_series_quality_mask(flux)
        differences = point_to_point_difference_ppt(flux, good_channels)
        if not differences.shape[0]:
            axis.text(
                0.5,
                0.5,
                "At least two integrations are required for point-to-point differences.",
                ha="center",
                va="center",
                transform=axis.transAxes,
            )
            axis.set(
                title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]"
            )
            axis.set_ylim(*window)
            continue
        limit = robust_ppt_limit(differences)
        image = axis.pcolormesh(
            elapsed_hours(spectra)[1:],
            wavelength,
            differences.T,
            shading="auto",
            cmap=spectroscopic_time_series_colormap(),
            norm=colors.TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit),
        )
        axis.set(title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
        axis.set_ylim(*window)
        axis.text(
            0.99,
            0.97,
            f"{retained_channel_label(good_channels)}\nIntegration pairs: {differences.shape[0]}",
            transform=axis.transAxes,
            ha="right",
            va="top",
            fontsize=8,
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 2},
        )
        fig.colorbar(image, ax=axis, label=POINT_TO_POINT_COLORBAR_LABEL)
    fig.suptitle(title)
    return save_figure(fig, output)


def _no_native_grid(axis, order: int, window: tuple[float, float]) -> None:
    """Annotate a panel when native grids cannot be stacked without interpolation."""
    axis.text(
        0.5,
        0.5,
        "Native wavelength grids differ; no interpolation applied.",
        ha="center",
        va="center",
        transform=axis.transAxes,
    )
    axis.set(title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
    axis.set_ylim(*window)


def _no_window_data(axis, order: int, window: tuple[float, float]) -> None:
    """Annotate a panel when no extracted channel falls in its configured window."""
    axis.text(
        0.5,
        0.5,
        "No extracted channels in configured wavelength window.",
        ha="center",
        va="center",
        transform=axis.transAxes,
    )
    axis.set(title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
    axis.set_ylim(*window)


def soss_title(header, stage: str) -> str:
    return product_title(header, f" | {stage} NIRISS/SOSS")
