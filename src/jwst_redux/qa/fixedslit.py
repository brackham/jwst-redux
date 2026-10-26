"""Read-only QA of official, nod-combined NIRSpec fixed-slit Stage-3 products.

An EXTRACT1D HDU is a spectrum, not a time series. Source/slit/order and detector
identities are never collapsed, and the official resampling is never repeated.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from astropy import units as u
from astropy.io import fits
from matplotlib.colors import AsinhNorm

from .checks import write_report
from .common import MAD_TO_SIGMA, finite_median, save_figure

PARAMETERS = {
    "implementation_version": 2,
    "spectra": "native flux units; finite positive wavelengths and DQ=0; no clipping of science",
    "snr": "signed flux / finite positive FLUX_ERROR in matching units; DQ=0",
    "line_limits_percentiles": [1, 99],
    "image_limits_percentiles": [1, 99],
    "display_mad_sigma_bound": 20,
    "image_normalization": "asinh; finite SCI, DQ=0 where present, WHT>0 where present",
    "gap_step_factor": 5,
    "grouping": "one panel per EXTRACT1D spectrum / SCI extension; one QA run per product pair",
}


@dataclass
class ExtractedSpectrum:
    wavelength: np.ndarray  # microns, converted using the table's actual TUNIT
    flux: np.ndarray  # native table units
    error: np.ndarray  # converted to the flux column's unit
    dq: np.ndarray
    header: fits.Header
    flux_unit: str
    wavelength_unit: str
    warnings: list[str]
    extension: int

    @property
    def valid(self) -> np.ndarray:
        return (
            np.isfinite(self.wavelength)
            & (self.wavelength > 0)
            & np.isfinite(self.flux)
            & (self.dq == 0)
        )

    @property
    def snr(self) -> np.ndarray:
        values = np.full(self.flux.shape, np.nan)
        good = self.valid & np.isfinite(self.error) & (self.error > 0)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            np.divide(self.flux, self.error, out=values, where=good)
        values[~np.isfinite(values)] = np.nan
        return values


@dataclass
class ResampledSpectrum:
    data: np.ndarray  # spatial, spectral; masked samples are NaN
    wavelength: np.ndarray | None  # microns; None means explicitly pixel-labelled
    header: fits.Header
    unit: str
    warnings: list[str]
    detectors: list[str]


def _metadata(header: fits.Header) -> dict[str, Any]:
    return {
        name: header.get(key)
        for name, key in {
            "program": "PROGRAM",
            "observation": "OBSERVTN",
            "target_name": "TARGNAME",
            "target_proposal": "TARGPROP",
            "instrument": "INSTRUME",
            "exposure_type": "EXP_TYPE",
            "detector": "DETECTOR",
            "grating": "GRATING",
            "filter": "FILTER",
            "primary_slit": "FXD_SLIT",
            "slit": "SLTNAME",
            "source_id": "SOURCEID",
            "source_type": "SRCTYPE",
            "order": "SPORDER",
        }.items()
    }


def title(header: fits.Header) -> str:
    target = header.get("TARGNAME") or header.get("TARGPROP") or "unknown target"
    return (
        f"{target} | Program {header.get('PROGRAM', '?')} Obs {header.get('OBSERVTN', '?')}"
        f" | {header.get('GRATING', '?')}/{header.get('FILTER', '?')}\n"
        f"Stage 3 combined | Slit {header.get('SLTNAME', header.get('FXD_SLIT', '?'))}"
        f" | Source {header.get('SOURCEID', '?')} ({header.get('SRCTYPE', '?')})"
        f" | {header.get('DETECTOR', '?')} | Order {header.get('SPORDER', '?')}"
    )


def read_x1d(path: Path) -> list[ExtractedSpectrum]:
    """Read every scalar-column table or vector-row spectrum without flattening sources."""
    spectra = []
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul[1:]:
            if hdu.name != "EXTRACT1D" or hdu.data is None:
                continue
            notes: list[str] = []
            header = hdul[0].header.copy()
            header.update(hdu.header)
            names = {n.upper(): n for n in hdu.columns.names}
            if not {"WAVELENGTH", "FLUX"} <= names.keys():
                raise ValueError(f"{path}: EXTRACT1D lacks WAVELENGTH/FLUX")
            wave_unit = hdu.columns[names["WAVELENGTH"]].unit or ""
            flux_unit = hdu.columns[names["FLUX"]].unit or ""
            # Unknown physical units must not yield a misleading wavelength or S/N axis.
            wavelength = (
                np.asarray(hdu.data[names["WAVELENGTH"]], float) * u.Unit(wave_unit)
            ).to_value(u.um)
            u.Unit(flux_unit)
            if not flux_unit:
                notes.append("FLUX unit missing; native values shown with unknown units")
            flux = np.asarray(hdu.data[names["FLUX"]], float)
            if wavelength.shape != flux.shape or flux.ndim not in {1, 2} or not flux.size:
                raise ValueError(f"{path}: malformed wavelength/flux shapes")
            error = np.full(flux.shape, np.nan)
            if "FLUX_ERROR" in names:
                try:
                    values = np.asarray(hdu.data[names["FLUX_ERROR"]], float)
                    if values.shape != flux.shape:
                        raise ValueError("shape differs from FLUX")
                    error_unit = hdu.columns[names["FLUX_ERROR"]].unit or ""
                    if not error_unit or not flux_unit:
                        raise ValueError("missing uncertainty or flux unit")
                    error = (values * u.Unit(error_unit)).to_value(u.Unit(flux_unit))
                except (ValueError, TypeError) as exc:
                    notes.append(f"Unusable FLUX_ERROR: {exc}")
            else:
                notes.append("FLUX_ERROR missing; S/N unavailable")
            dq = np.zeros(flux.shape, dtype=np.uint32)
            if "DQ" in names:
                dq = np.asarray(hdu.data[names["DQ"]])
                if dq.shape != flux.shape:
                    notes.append("Malformed DQ shape; all samples masked")
                    dq = np.ones(flux.shape, dtype=np.uint32)
            else:
                notes.append("DQ missing; only finite-value masks available")
            rows = range(flux.shape[0]) if flux.ndim == 2 else [None]
            for row in rows:

                def take(a, row=row):
                    return a.copy() if row is None else a[row].copy()

                spectrum_header = header.copy()
                if row is not None:
                    for column, keyword in (
                        ("SOURCE_ID", "SOURCEID"),
                        ("SLIT_NAME", "SLTNAME"),
                        ("SOURCE_TYPE", "SRCTYPE"),
                        ("SPORDER", "SPORDER"),
                        ("DETECTOR", "DETECTOR"),
                    ):
                        if column in names:
                            value = hdu.data[names[column]][row]
                            if np.ndim(value) == 0:
                                spectrum_header[keyword] = (
                                    value.item() if hasattr(value, "item") else value
                                )
                spectra.append(
                    ExtractedSpectrum(
                        take(wavelength),
                        take(flux),
                        take(error),
                        take(dq),
                        spectrum_header,
                        flux_unit,
                        wave_unit,
                        list(notes),
                        int(hdu.header.get("EXTVER", 1)),
                    )
                )
    if not spectra:
        raise ValueError(f"{path}: no EXTRACT1D spectra")
    return spectra


def _wcs_wavelength(path: Path, index: int, shape: tuple[int, ...]) -> np.ndarray:
    """Evaluate the official GWCS, including its declared spectral units."""
    from jwst import datamodels

    with datamodels.open(path) as model:
        slit = model.slits[index] if hasattr(model, "slits") else model
        wcs = slit.meta.wcs
        spectral = list(wcs.output_frame.axes_type).index("SPECTRAL")
        y, x = np.indices(shape)
        return (np.asarray(wcs(x, y)[spectral]) * wcs.output_frame.unit[spectral]).to_value(u.um)


def read_s2d(path: Path) -> list[ResampledSpectrum]:
    """Read every SCI extension; WHT supplies the normal resampled no-coverage mask."""
    images = []
    with fits.open(path, memmap=False) as hdul:
        detectors = []
        if "HDRTAB" in hdul and "DETECTOR" in hdul["HDRTAB"].columns.names:
            detectors = sorted({str(x).strip() for x in hdul["HDRTAB"].data["DETECTOR"]})
        for hdu in hdul[1:]:
            if hdu.name != "SCI" or hdu.data is None:
                continue
            notes = []
            header = hdul[0].header.copy()
            header.update(hdu.header)
            data = np.array(hdu.data, dtype=float)
            if data.ndim != 2 or not data.size:
                raise ValueError(f"{path}: SCI must be a nonempty 2D image")
            version = hdu.header.get("EXTVER", 1)
            for name in ("DQ", "WHT"):
                if (name, version) in hdul:
                    mask = hdul[name, version].data
                    if mask is None or mask.shape != data.shape:
                        raise ValueError(f"{path}: malformed {name} image")
                    bad = mask != 0 if name == "DQ" else ~np.isfinite(mask) | (mask <= 0)
                    data[bad] = np.nan
                elif name == "WHT":
                    notes.append("WHT missing; no resampling weight/coverage mask available")
            data[~np.isfinite(data)] = np.nan
            if not np.any(np.isfinite(data)):
                notes.append("No usable resampled SCI pixels")
            wavelength = None
            try:
                if ("WAVELENGTH", version) in hdul and hdul["WAVELENGTH", version].header.get(
                    "BUNIT"
                ):
                    wave_hdu = hdul["WAVELENGTH", version]
                    grid = (np.asarray(wave_hdu.data) * u.Unit(wave_hdu.header["BUNIT"])).to_value(
                        u.um
                    )
                else:
                    grid = _wcs_wavelength(path, len(images), data.shape)
                if grid.shape != data.shape:
                    raise ValueError("wavelength grid shape differs from SCI")
                if header.get("DISPAXIS", 1) == 2:
                    grid = grid.T
                wave = np.asarray(finite_median(grid, axis=0))
                if (
                    not np.all(np.isfinite(wave) & (wave > 0))
                    or not (np.all(np.diff(wave) > 0) or np.all(np.diff(wave) < 0))
                    or not np.allclose(grid, wave[None, :], rtol=1e-5, equal_nan=False)
                ):
                    raise ValueError("wavelength grid is not finite, monotonic and rectified")
                wavelength = wave
            except Exception as exc:  # noqa: BLE001 - retain an honestly pixel-labelled diagnostic.
                notes.append(f"Physical wavelength axis unavailable; using resampled pixels: {exc}")
            if header.get("DISPAXIS", 1) == 2:
                data = data.T
            images.append(
                ResampledSpectrum(
                    data, wavelength, header, header.get("BUNIT", "unknown"), notes, detectors
                )
            )
    if not images:
        raise ValueError(f"{path}: no SCI images")
    return images


def _intervals(wavelength: np.ndarray, valid: np.ndarray) -> list[list[float]]:
    """Intervals of usable samples, preserving bad channels and large coordinate gaps."""
    intervals = []
    for indices in _runs(wavelength, valid):
        intervals.append([float(np.min(wavelength[indices])), float(np.max(wavelength[indices]))])
    return intervals


def _runs(wave: np.ndarray, valid: np.ndarray) -> list[np.ndarray]:
    delta = np.abs(np.diff(wave))
    positive = delta[np.isfinite(delta) & (delta > 0)]
    gap = (
        delta > PARAMETERS["gap_step_factor"] * np.median(positive)
        if positive.size
        else np.zeros(delta.shape, bool)
    )
    indices = np.flatnonzero(valid)
    if not indices.size:
        return []
    breaks = (np.diff(indices) != 1) | gap[indices[:-1]]
    return list(np.split(indices, np.flatnonzero(breaks) + 1))


def _summary(spectrum: ExtractedSpectrum) -> dict[str, Any]:
    n = spectrum.flux.size
    snr = spectrum.snr
    finite_snr = snr[np.isfinite(snr)]
    notes = list(spectrum.warnings)
    if not np.any(spectrum.valid):
        notes.append("No usable extracted flux")
    if not finite_snr.size:
        notes.append("No usable positive uncertainties; S/N unavailable")
    wave = spectrum.wavelength[np.isfinite(spectrum.wavelength) & (spectrum.wavelength > 0)]
    ordered = bool(wave.size and (np.all(np.diff(wave) > 0) or np.all(np.diff(wave) < 0)))
    if not ordered:
        notes.append("Wavelength samples are empty, repeated or non-monotonic; not reordered")
    return {
        "metadata": _metadata(spectrum.header),
        "extension": spectrum.extension,
        "samples": n,
        "flux_unit": spectrum.flux_unit,
        "input_wavelength_unit": spectrum.wavelength_unit,
        "wavelength_unit": "um",
        "wavelength_range": [float(wave.min()), float(wave.max())] if wave.size else None,
        "monotonic_wavelength": ordered,
        "usable_wavelength_intervals": _intervals(spectrum.wavelength, spectrum.valid),
        "counts": {
            "dq_flagged": int(np.count_nonzero(spectrum.dq)),
            "finite_flux": int(np.count_nonzero(np.isfinite(spectrum.flux))),
            "finite_uncertainty": int(np.count_nonzero(np.isfinite(spectrum.error))),
            "positive_finite_uncertainty": int(
                np.count_nonzero(np.isfinite(spectrum.error) & (spectrum.error > 0))
            ),
            "invalid_or_flagged": int(np.count_nonzero(~spectrum.valid)),
            "usable_snr": int(finite_snr.size),
        },
        "invalid_or_flagged_fraction": float(np.mean(~spectrum.valid)),
        "dq_flagged_fraction": float(np.mean(spectrum.dq != 0)),
        "finite_flux_fraction": float(np.mean(np.isfinite(spectrum.flux))),
        "finite_uncertainty_fraction": float(np.mean(np.isfinite(spectrum.error))),
        "snr": dict(
            zip(("p16", "median", "p84"), map(float, np.percentile(finite_snr, [16, 50, 84])))
        )
        if finite_snr.size
        else None,
        "warnings": notes,
    }


def _line_plot(spectra: list[ExtractedSpectrum], output: Path, *, snr: bool) -> Path:
    fig, axes = plt.subplots(len(spectra), 1, figsize=(12, 4.5 * len(spectra)), squeeze=False)
    for axis, spectrum in zip(axes[:, 0], spectra, strict=True):
        values = spectrum.snr if snr else np.where(spectrum.valid, spectrum.flux, np.nan)
        for indices in _runs(spectrum.wavelength, np.isfinite(values)):
            axis.plot(
                spectrum.wavelength[indices],
                values[indices],
                color="C0",
                linewidth=0.7,
                marker="." if len(indices) == 1 else None,
            )
        if np.any(np.isfinite(values)):
            low, high = _display_limits(values)
            margin = 0.08 * (high - low)
            axis.set_ylim(low - margin, high + margin)
        else:
            axis.text(
                0.5,
                0.5,
                "No usable S/N" if snr else "No usable flux",
                transform=axis.transAxes,
                ha="center",
            )
        finite_wave = spectrum.wavelength[
            np.isfinite(spectrum.wavelength) & (spectrum.wavelength > 0)
        ]
        if finite_wave.size > 1:
            axis.set_xlim(float(np.min(finite_wave)), float(np.max(finite_wave)))
        axis.set(
            title=title(spectrum.header),
            xlabel="Wavelength [µm]",
            ylabel="S/N per spectral sample"
            if snr
            else f"Flux [{spectrum.flux_unit or 'unknown unit'}]",
        )
        axis.text(
            0.01,
            0.03,
            "DQ≠0 and invalid samples masked; robust display limits (see checks.json)",
            transform=axis.transAxes,
            fontsize=8,
        )
    return save_figure(fig, output)


def _image_plot(images: list[ResampledSpectrum], output: Path) -> Path:
    fig, axes = plt.subplots(len(images), 1, figsize=(12, 4.5 * len(images)), squeeze=False)
    for axis, image in zip(axes[:, 0], images, strict=True):
        low, high = _display_limits(image.data)
        norm = AsinhNorm(
            linear_width=max((high - low) / 10, np.finfo(float).tiny), vmin=low, vmax=high
        )
        kwargs = {"cmap": plt.get_cmap("viridis").with_extremes(bad="0.75"), "norm": norm}
        if image.wavelength is None:
            artist = axis.imshow(
                np.ma.masked_invalid(image.data),
                origin="lower",
                aspect="auto",
                interpolation="nearest",
                **kwargs,
            )
            xlabel = "Spectral coordinate [resampled pixel]"
        else:
            # Split coordinate discontinuities instead of painting across a detector gap.
            for indices in _runs(image.wavelength, np.ones(image.wavelength.size, bool)):
                artist = axis.pcolormesh(
                    image.wavelength[indices],
                    np.arange(image.data.shape[0]),
                    np.ma.masked_invalid(image.data[:, indices]),
                    shading="nearest",
                    **kwargs,
                )
            xlabel = "Wavelength [µm]"
        bar = fig.colorbar(artist, ax=axis, label=f"SCI [{image.unit}]")
        ticks = norm.inverse(np.linspace(0, 1, 5))
        bar.set_ticks(ticks, labels=[f"{v:.2g}" for v in ticks])
        axis.set_facecolor("0.85")
        if not np.any(np.isfinite(image.data)):
            axis.text(0.5, 0.5, "No usable SCI pixels", transform=axis.transAxes, ha="center")
        axis.set(
            title=title(image.header), xlabel=xlabel, ylabel="Spatial coordinate [resampled pixel]"
        )
    return save_figure(fig, output)


def _display_limits(values: np.ndarray) -> tuple[float, float]:
    """Percentile limits bounded against isolated extremes, including tiny fluxes."""
    finite = values[np.isfinite(values)]
    if not finite.size:
        return 0.0, 1.0
    center = float(np.median(finite))
    sigma = MAD_TO_SIGMA * float(np.median(np.abs(finite - center)))
    bound = PARAMETERS["display_mad_sigma_bound"] * sigma
    if not bound:
        bound = 0.01 * abs(center) if center else 1.0
    low, high = np.percentile(finite, [1, 99], method="nearest")
    low, high = max(float(low), center - bound), min(float(high), center + bound)
    return (low, high) if low < high else (center - bound, center + bound)


def generate(x1d: Path, s2d: Path, output_dir: Path) -> tuple[Path, ...]:
    """Generate diagnostics, preserving warnings even when inputs cannot be plotted."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "inputs": [str(x1d), str(s2d)],
        "parameters": PARAMETERS,
        "warnings": [],
    }
    spectra, images = [], []
    for reader, path, destination in ((read_x1d, x1d, spectra), (read_s2d, s2d, images)):
        try:
            destination.extend(reader(path))
        except Exception as exc:  # noqa: BLE001 - report malformed scientific products explicitly.
            report["warnings"].append(f"{path.name}: {exc}")
    report["spectra"] = [_summary(s) for s in spectra]
    report["images"] = [
        {
            "metadata": _metadata(i.header),
            "shape": list(i.data.shape),
            "unit": i.unit,
            "input_detectors": i.detectors,
            "finite_fraction": float(np.mean(np.isfinite(i.data))),
            "physical_wavelength_axis": i.wavelength is not None,
            "warnings": i.warnings,
        }
        for i in images
    ]
    usable = bool(
        spectra
        and images
        and any(np.any(s.valid) for s in spectra)
        and any(np.any(np.isfinite(i.data)) for i in images)
    )
    report["checks"] = {
        "usable_extracted_flux_and_image": usable,
        "usable_snr": any(np.any(np.isfinite(s.snr)) for s in spectra),
    }
    spectra_ids = {(s.header.get("SLTNAME"), s.header.get("SOURCEID")) for s in spectra}
    image_ids = {(i.header.get("SLTNAME"), i.header.get("SOURCEID")) for i in images}
    if spectra_ids != image_ids:
        report["warnings"].append("Extracted/resampled source and slit identifiers disagree")
    report["quality_status"] = (
        "unusable"
        if not usable
        else "warning"
        if any(s["warnings"] for s in report["spectra"]) or any(i.warnings for i in images)
        else "pass"
    )
    report_path = output_dir / "checks.json"
    write_report(report, report_path)
    if report["warnings"] or not spectra or not images:
        raise ValueError(f"Unusable fixed-slit products; see {report_path}")
    return (
        _line_plot(spectra, output_dir / "spectra.png", snr=False),
        _line_plot(spectra, output_dir / "snr_spectrum.png", snr=True),
        _image_plot(images, output_dir / "spectrum_2d.png"),
        report_path,
    )
