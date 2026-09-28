"""Visit-level NIRISS/SOSS QA from combined TSO3 products."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.table import Table

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


def official_white_light(path: Path) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Discover official per-order ECSV columns and convert available time to hours."""
    table = Table.read(path, format="ascii.ecsv")
    names = {name.lower(): name for name in table.colnames}
    time_name = names.get("bjd_tdb") or names.get("mjd_utc")
    if time_name is None:
        raise ValueError(f"Official white-light table has no BJD_TDB or MJD_UTC: {path}")
    time = np.asarray(table[time_name], dtype=float)
    elapsed = (time - np.nanmin(time)) * 24.0
    outputs: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for name in table.colnames:
        lower = name.lower()
        if not lower.startswith("whitelight_flux_order_"):
            continue
        try:
            order = int(lower.rsplit("_", 1)[1])
        except ValueError:
            continue
        outputs[order] = (elapsed, np.asarray(table[name], dtype=float))
    if not outputs:
        raise ValueError(
            f"Official white-light table has no whitelight_flux_order_N columns: {path}"
        )
    return outputs


def generate(
    x1dints_path: Path,
    whtlt_path: Path,
    output_dir: Path,
    wavelength_windows: dict[int, tuple[float, float]],
    *,
    quicklook_cadence_minutes: float = 2.0,
) -> tuple[Path, ...]:
    groups, header, flux_unit, _ = spectra_from_x1dints(x1dints_path)
    title = soss_title(header, "Stage 3")
    quicklook = white_light_quicklook_products(
        groups,
        title,
        output_dir,
        cadence_minutes=quicklook_cadence_minutes,
        stage="Stage 3",
    )
    outputs = (
        spectra_plot(groups, title, flux_unit, output_dir / "spectra.png", wavelength_windows),
        white_light_proxy_plot(
            groups, title, output_dir / "white_light.png", official=official_white_light(whtlt_path)
        ),
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
