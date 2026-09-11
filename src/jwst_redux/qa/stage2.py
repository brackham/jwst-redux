"""Per-segment NIRISS/SOSS QA from Stage 2 x1dints files."""

from __future__ import annotations

from pathlib import Path

from .common import spectra_from_x1dints
from .soss import dynamic_spectrum_plot, soss_title, spectra_plot, white_light_proxy_plot


def generate(path: Path, output_dir: Path) -> tuple[Path, ...]:
    groups, header, flux_unit, _ = spectra_from_x1dints(path)
    title = soss_title(header, "Stage 2")
    return (
        spectra_plot(groups, title, flux_unit, output_dir / "spectra.png"),
        white_light_proxy_plot(groups, title, output_dir / "white_light.png"),
        dynamic_spectrum_plot(groups, title, output_dir / "dynamic_spectrum.png"),
    )
