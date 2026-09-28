"""Per-segment NIRISS/SOSS QA from Stage 2 x1dints files."""

from __future__ import annotations

from pathlib import Path

from .common import spectra_from_x1dints
from .soss import (
    plot_point_to_point_difference,
    plot_scatter_spectrum,
    plot_spectroscopic_time_series,
    soss_title,
    spectra_plot,
    white_light_proxy_plot,
    white_light_quicklook_products,
)


def generate(
    path: Path,
    output_dir: Path,
    wavelength_windows: dict[int, tuple[float, float]],
    *,
    quicklook_cadence_minutes: float = 2.0,
) -> tuple[Path, ...]:
    groups, header, flux_unit, _ = spectra_from_x1dints(path)
    title = soss_title(header, "Stage 2")
    quicklook = white_light_quicklook_products(
        groups,
        title,
        output_dir,
        cadence_minutes=quicklook_cadence_minutes,
        stage="Stage 2",
    )
    outputs = (
        spectra_plot(groups, title, flux_unit, output_dir / "spectra.png", wavelength_windows),
        white_light_proxy_plot(groups, title, output_dir / "white_light.png"),
        *quicklook,
        plot_spectroscopic_time_series(
            groups,
            title,
            output_dir / "spectroscopic_time_series.png",
            wavelength_windows,
        ),
        plot_scatter_spectrum(
            groups,
            title,
            output_dir / "scatter_spectrum.png",
            wavelength_windows,
        ),
        plot_point_to_point_difference(
            groups,
            title,
            output_dir / "point_to_point_difference.png",
            wavelength_windows,
        ),
    )
    return outputs
