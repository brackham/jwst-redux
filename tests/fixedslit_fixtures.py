"""Small scalar-row EXTRACT1D and rectified SCI products, following JWST 3 layouts."""

from pathlib import Path

import numpy as np
from astropy.io import fits


def write_pair(root: Path, header: fits.Header, *, sources=1, vector_rows=False):
    wave = np.arange(100, dtype=float) / 100 + 1
    flux = 0.01 + np.sin(wave) / 1000
    error = np.full(100, 0.001)
    dq = np.zeros(100, np.uint32)
    flux[30:40] = np.nan  # retained official grid across a detector gap
    dq[30:40] = 1
    flux[10] = np.inf
    error[11:14] = [0, -1, np.nan]
    dq[15] = 2
    flux[-1] = 1e20  # pathological but unflagged; display limits, never science edits
    extensions = []
    images = []
    for index in range(sources):
        meta = fits.Header(
            {
                "EXTVER": index + 1,
                "SLTNAME": f"S200A{index + 1}",
                "SOURCEID": index + 1,
                "SRCTYPE": "POINT" if index == 0 else "EXTENDED",
                "SPORDER": 1,
                "DISPAXIS": 1,
                "DETECTOR": "MULTIPLE",
            }
        )
        columns = []
        for name, values, unit in (
            ("WAVELENGTH", wave, "um"),
            ("FLUX", flux, "Jy"),
            ("FLUX_ERROR", error, "Jy"),
            ("DQ", dq, None),
        ):
            fmt = "J" if name == "DQ" else "D"
            array = np.stack([values, values]) if vector_rows else values
            columns.append(
                fits.Column(
                    name=name, format=f"100{fmt}" if vector_rows else fmt, array=array, unit=unit
                )
            )
        extensions.append(fits.BinTableHDU.from_columns(columns, header=meta, name="EXTRACT1D"))
        data = np.tile(np.linspace(-0.001, 0.001, 5)[:, None], (1, 100))
        data[2] += np.nan_to_num(flux, posinf=0)
        weight = np.ones_like(data)
        weight[:, 30:40] = 0
        data[0, 0] = np.inf
        meta["BUNIT"] = "MJy"
        images.extend(
            [
                fits.ImageHDU(data, header=meta, name="SCI"),
                fits.ImageHDU(weight, header=fits.Header({"EXTVER": index + 1}), name="WHT"),
                fits.ImageHDU(
                    np.tile(wave, (5, 1)),
                    header=fits.Header({"BUNIT": "um", "EXTVER": index + 1}),
                    name="WAVELENGTH",
                ),
            ]
        )
    detector_table = fits.BinTableHDU.from_columns(
        [fits.Column(name="DETECTOR", format="4A", array=["NRS1", "NRS2"] * 3)], name="HDRTAB"
    )
    x1d, s2d = Path(str(root) + "_x1d.fits"), Path(str(root) + "_s2d.fits")
    fits.HDUList([fits.PrimaryHDU(header=header), *extensions]).writeto(x1d, overwrite=True)
    fits.HDUList([fits.PrimaryHDU(header=header), *images, detector_table]).writeto(
        s2d, overwrite=True
    )
    return x1d, s2d
