"""Persist official SOSS extraction results when photometric calibration is skipped."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from jwst.stpipe import Step

from ..exceptions import PipelineExecutionError

SOSS_SAVE_HOOK = "jwst_redux.pipeline.soss.SaveUncalibratedSoss"


def spec2_parameters(overrides: dict[str, Any]) -> dict[str, Any]:
    """Add a supported stpipe hook, preserving caller parameters and other hooks.

    Install even when skip is resolved from CRDS rather than explicit YAML.
    The hook checks the resolved step state and exposure type at runtime.
    """
    parameters = deepcopy(overrides)
    photom = parameters.setdefault("steps", {}).setdefault("photom", {})
    hooks = list(photom.get("post_hooks", []))
    if SOSS_SAVE_HOOK not in hooks:
        hooks.append(SOSS_SAVE_HOOK)
    photom["post_hooks"] = hooks
    return parameters


class SaveUncalibratedSoss(Step):
    """Save the existing extraction model after the official skipped photom step.

    Spec2 disables extract_1d saving for SOSS, expecting photom to save instead.
    stpipe runs post-hooks for skipped steps but does not save their results.
    No extraction, calibration, unit conversion, or array modification occurs here.
    """

    def process(self, model):
        photom = self.parent
        if (
            model is not None
            and photom.skip
            and model.meta.exposure.type == "NIS_SOSS"
            and model.meta.cal_step.extract_1d == "COMPLETE"
            and model.meta.cal_step.photom == "SKIPPED"
        ):
            # Use the parent pipeline's naming and output directory. In particular,
            # a single integration in a rateints cube still needs x1dints.
            pipeline = photom.parent
            path = Path(pipeline.make_output_path(suffix="x1dints"))
            print(f"jwst-redux: saving uncalibrated SOSS extraction to {path}")
            pipeline.save_model(model, suffix="x1dints", force=True)
        return model


def validate_soss_stage2(
    input_path: Path, outputs: tuple[Path, ...], overrides: dict[str, Any]
) -> None:
    """Require readable, integration-resolved SOSS spectra, including on resume.

    Support both the older per-integration EXTRACT1D extensions and JWST 3's
    vector tables with an INT_NUM column. Read FITS directly without CRDS access.
    """
    from astropy.io import fits

    try:
        with fits.open(input_path) as source:
            header = source[0].header
            if header.get("EXP_TYPE") != "NIS_SOSS":
                return
            count = source["SCI"].data.shape[0]
        with fits.open(outputs[0]) as calibrated:
            calibrated.verify("exception")
            if calibrated["SCI"].data.shape[0] != count:
                raise ValueError("calints integration count differs from rateints")
        with fits.open(outputs[1]) as spectra:
            spectra.verify("exception")
            if spectra[0].header.get("S_EXTR1D") != "COMPLETE":
                raise ValueError("spectral extraction is not COMPLETE")
            if (
                overrides.get("steps", {}).get("photom", {}).get("skip") is True
                and spectra[0].header.get("S_PHOTOM") != "SKIPPED"
            ):
                raise ValueError("photom was not skipped as requested")
            tables = [hdu for hdu in spectra if hdu.name == "EXTRACT1D"]
            if not tables:
                raise ValueError("no EXTRACT1D spectra")
            integrations: dict[int, set[int]] = {}
            for hdu in tables:
                data = hdu.data
                if data is None or not len(data) or not {"WAVELENGTH", "FLUX"} <= set(data.names):
                    raise ValueError("empty or malformed spectral table")
                order = hdu.header.get("SPORDER", 1)
                if "INT_NUM" in data.names:
                    ids = set(map(int, data["INT_NUM"]))
                    if len(ids) != len(data) or min(ids) < 1:
                        raise ValueError("invalid or repeated integration identifiers")
                    if any(row.size == 0 for row in data["FLUX"]):
                        raise ValueError("empty extracted flux")
                else:
                    ids = {int(hdu.header["INT_NUM"])}
                integrations.setdefault(order, set()).update(ids)
            if any(
                len(ids) != count or min(ids) < 1 or max(ids) - min(ids) + 1 != count
                for ids in integrations.values()
            ):
                raise ValueError("spectra do not cover every segment integration")
    except Exception as error:
        raise PipelineExecutionError(f"Invalid SOSS Stage 2 products: {error}") from error
