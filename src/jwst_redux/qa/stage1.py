"""Detector-level QA products from Stage 1 rateints files."""

from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits

from .common import MAD_TO_SIGMA, finite_median, product_title, robust_limits, save_figure


def generate(path: Path, output_dir: Path) -> tuple[Path, ...]:
    """Create median-image, normalized-total, and robust-scatter diagnostics."""
    # ``DQ`` is commonly an unsigned FITS image (BZERO) which Astropy cannot
    # memory-map after scaling.  Reading its unscaled representation preserves
    # the efficient SCI mapping and lets us identify the stored zero value.
    with fits.open(path, memmap=True, do_not_scale_image_data=True) as hdul:
        header = hdul[0].header.copy()
        data = np.asarray(hdul["SCI"].data, dtype=float)
        dq = None
        if "DQ" in hdul:
            dq_hdu = hdul["DQ"]
            raw_dq = np.asarray(dq_hdu.data)
            stored_zero = -int(dq_hdu.header.get("BZERO", 0))
            dq = raw_dq != stored_zero
        unit = hdul["SCI"].header.get("BUNIT", header.get("BUNIT", "DN/s"))
    if data.ndim != 3:
        raise ValueError(f"Stage 1 SCI data must be (integration, y, x), got {data.shape}")
    usable = data.copy()
    if dq is not None and dq.shape == usable.shape:
        usable[dq] = np.nan
    median = finite_median(usable, axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        scatter = MAD_TO_SIGMA * np.nanmedian(np.abs(usable - median), axis=0)
    totals = np.nansum(usable, axis=(1, 2))
    totals[~np.isfinite(totals)] = np.nan
    baseline = float(finite_median(totals))
    normalized = totals / baseline if np.isfinite(baseline) and baseline != 0 else totals
    segment = (
        path.name.split("-seg", maxsplit=1)[1].split("_", maxsplit=1)[0]
        if "-seg" in path.name
        else "?"
    )
    title = product_title(header, f" | Stage 1 rateints | Segment {segment}")
    outputs: list[Path] = []
    for image, name, label in (
        (median, "median_detector.png", f"Temporal median [{unit}]"),
        (scatter, "temporal_scatter.png", f"MAD scatter [{unit}]"),
    ):
        fig, ax = plt.subplots(figsize=(9, 4.5))
        low, high = robust_limits(image)
        im = ax.imshow(image, origin="lower", aspect="auto", cmap="viridis", vmin=low, vmax=high)
        ax.set(title=title, xlabel="Detector x [pixel]", ylabel="Detector y [pixel]")
        fig.colorbar(im, ax=ax, label=label)
        outputs.append(save_figure(fig, output_dir / name))
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(np.arange(1, len(normalized) + 1), normalized, marker=".", linewidth=1)
    ax.axhline(1, color="0.4", linewidth=1)
    ax.set(title=title, xlabel="Integration number", ylabel="Normalized unflagged SCI sum")
    ax.text(
        0.01,
        0.02,
        "Sum of finite SCI pixels with DQ = 0; normalized by temporal median.",
        transform=ax.transAxes,
        fontsize=8,
        va="bottom",
    )
    outputs.append(save_figure(fig, output_dir / "flux_vs_integration.png"))
    return tuple(outputs)
