"""SOSS extracted-spectrum plotting shared by Stage 2 and Stage 3 QA."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import colors

from .common import (
    Spectrum,
    dynamic_spectrum_display,
    elapsed_hours,
    finite_median,
    flux_to_f_lambda,
    product_title,
    retained_channel_label,
    retained_wavelength_extent,
    save_figure,
    valid_flux,
)

DYNAMIC_COLORBAR_LABEL = "Relative flux [ppt]"


def dynamic_colormap():
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


def spectra_plot(
    groups: dict[int, list[Spectrum]], title: str, flux_unit: str, output: Path
) -> Path:
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.2 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
        for item in spectra:
            valid = valid_flux(item)
            display_flux = flux_to_f_lambda(item.flux, item.wavelength, flux_unit)
            axis.plot(
                item.wavelength[valid],
                display_flux[valid],
                color="C0",
                alpha=min(0.25, 8 / max(len(spectra), 1)),
                linewidth=0.5,
            )
        stack = _masked_stack(spectra)
        if stack is not None:
            wavelength, flux = stack
            median = flux_to_f_lambda(finite_median(flux, axis=0), wavelength, flux_unit)
            valid = np.isfinite(wavelength) & np.isfinite(median)
            axis.plot(
                wavelength[valid], median[valid], color="C1", linewidth=1.5, label="Temporal median"
            )
            _, good_channels, _ = dynamic_spectrum_display(flux)
            extent = retained_wavelength_extent(wavelength, good_channels)
            if extent is not None:
                for boundary in extent:
                    axis.axvline(boundary, color="0.35", linestyle=":", linewidth=0.8, alpha=0.7)
        axis.set(
            title=f"Order {order}",
            xlabel="Wavelength [µm]",
            ylabel="Fλ [W m⁻² µm⁻¹]",
        )
        axis.legend(loc="best")
    fig.suptitle(title)
    return save_figure(fig, output)


def white_light_proxy_plot(
    groups: dict[int, list[Spectrum]],
    title: str,
    output: Path,
    *,
    official: dict[int, tuple[np.ndarray, np.ndarray]] | None = None,
) -> Path:
    fig, ax = plt.subplots(figsize=(10, 4.8))
    if official is not None:
        for order, (times, flux) in sorted(official.items()):
            baseline = finite_median(flux)
            ax.plot(times, flux / baseline, marker=".", linewidth=1, label=f"Order {order}")
        ylabel = "Normalized official TSO3 white-light flux"
    else:
        for order, spectra in sorted(groups.items()):
            values = np.array(
                [np.nansum(np.where(valid_flux(item), item.flux, np.nan)) for item in spectra]
            )
            baseline = finite_median(values)
            ax.plot(
                elapsed_hours(spectra),
                values / baseline,
                marker=".",
                linewidth=1,
                label=f"Order {order}",
            )
        ylabel = "Normalized QA-derived white-light proxy"
        ax.text(
            0.01,
            0.02,
            "QA-derived: sum of unflagged finite extracted flux samples.",
            transform=ax.transAxes,
            fontsize=8,
            va="bottom",
        )
    ax.axhline(1, color="0.4", linewidth=1)
    ax.set(title=title, xlabel="Elapsed time [hours]", ylabel=ylabel)
    ax.legend(loc="best")
    return save_figure(fig, output)


def dynamic_spectrum_plot(groups: dict[int, list[Spectrum]], title: str, output: Path) -> Path:
    fig, axes = plt.subplots(len(groups), 1, figsize=(11, 3.4 * len(groups)), squeeze=False)
    for axis, (order, spectra) in zip(axes[:, 0], sorted(groups.items())):
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
            continue
        wavelength, flux = stack
        residual, good_channels, limit = dynamic_spectrum_display(flux)
        extent = retained_wavelength_extent(wavelength, good_channels)
        cmap = dynamic_colormap()
        image = axis.pcolormesh(
            elapsed_hours(spectra),
            wavelength,
            residual.T,
            shading="auto",
            cmap=cmap,
            norm=colors.TwoSlopeNorm(vcenter=0, vmin=-limit, vmax=limit),
        )
        axis.set(title=f"Order {order}", xlabel="Elapsed time [hours]", ylabel="Wavelength [µm]")
        if extent is not None:
            axis.set_ylim(*extent)
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
        fig.colorbar(image, ax=axis, label=DYNAMIC_COLORBAR_LABEL)
    fig.suptitle(title)
    return save_figure(fig, output)


def soss_title(header, stage: str) -> str:
    return product_title(header, f" | {stage} NIRISS/SOSS")
