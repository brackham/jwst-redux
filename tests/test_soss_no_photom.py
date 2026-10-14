"""Exercise actual Spec2 save/skip behavior without reference data or ATOCA fits."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from configobj import ConfigObj
from jwst.pipeline import Spec2Pipeline
from stdatamodels.jwst import datamodels
from stpipe import Step
from test_stage2 import FakeDetector1, FakeDownloader, _arguments, _case

from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs, run_spec2
from jwst_redux.pipeline.soss import SOSS_SAVE_HOOK, spec2_parameters, validate_soss_stage2
from jwst_redux.stage1 import run_selected_through
from jwst_redux.workspace import Workspace

SKIP_PHOTOM = {"steps": {"photom": {"skip": True}}}


def _spectrum(nints, *, legacy=False):
    """Build small official extraction models in either supported table format."""
    model = datamodels.MultiSpecModel()
    for integration in range(1, nints + 1):
        spec = datamodels.SpecModel()
        spec.spec_table = np.zeros(3, dtype=spec.get_dtype("spec_table"))
        spec.spec_table["WAVELENGTH"] = [1.0, 1.1, 1.2]
        spec.spec_table["FLUX"] = [11.0, 12.0, 13.0]
        spec.spec_table.columns["FLUX"].unit = "DN/s"
        spec.spectral_order = 1
        spec.int_num = integration
        model.spec.append(spec)
    if not legacy and hasattr(datamodels, "TSOMultiSpecModel"):
        from jwst.datamodels.utils.tso_multispec import make_tso_specmodel

        vector = datamodels.TSOMultiSpecModel()
        vector.spec.append(make_tso_specmodel(model.spec, segment=1))
        model.close()
        model = vector
    model.meta.exposure.type = "NIS_SOSS"
    model.meta.cal_step.extract_1d = "COMPLETE"
    return model


@pytest.fixture
def official_spec2(monkeypatch):
    """Use real Spec2.process, extract/photom.run, hooks and FITS serialization.

    Replace expensive science computations only; all skip/save decisions remain
    those of the installed official pipeline. Disable CRDS/network access.
    """
    calls = {"extract": 0, "photom": 0}
    monkeypatch.setattr(Step, "get_config_from_reference", classmethod(lambda *a, **k: ConfigObj()))
    monkeypatch.setattr(Spec2Pipeline, "prefetch", lambda *a: None)
    monkeypatch.setattr(Spec2Pipeline, "_step_verification", lambda *a: None)
    monkeypatch.setattr(Spec2Pipeline, "_process_niriss_soss", lambda self, data: data)

    def assign(self, data):
        data.meta.cal_step.assign_wcs = "COMPLETE"
        return data

    def extract(self, data):
        calls["extract"] += 1
        assert not self.save_results  # Spec2 suppresses SOSS extraction saving
        return _spectrum(data.data.shape[0])

    def photom(self, model):
        calls["photom"] += 1
        model.meta.cal_step.photom = "COMPLETE"
        for spec in model.spec:
            spec.spec_table["FLUX"] *= 2  # sentinel for the enabled calibration path
            spec.spec_table.columns["FLUX"].unit = "Jy"
        return model

    from jwst.assign_wcs import AssignWcsStep
    from jwst.extract_1d import Extract1dStep
    from jwst.photom import PhotomStep

    monkeypatch.setattr(AssignWcsStep, "process", assign)
    monkeypatch.setattr(Extract1dStep, "process", extract)
    monkeypatch.setattr(PhotomStep, "process", photom)
    # Skip preceding science steps via their official run path.
    steps = {
        name: {"skip": True}
        for name in Spec2Pipeline.step_defs
        if name not in {"assign_wcs", "extract_1d", "photom"}
    }
    return calls, {"steps": steps}


def _rateints(path, nints=1):
    with datamodels.CubeModel((nints, 2, 3)) as model:
        model.meta.exposure.type = "NIS_SOSS"
        model.meta.instrument.name = "NIRISS"
        model.meta.instrument.filter = "CLEAR"
        model.meta.instrument.pupil = "GR700XD"
        model.meta.exposure.nints = nints
        model.save(path)


@pytest.mark.parametrize("nints", [1, 3])
@pytest.mark.parametrize("skip", [False, True])
def test_official_spec2_persists_integration_spectra(tmp_path, official_spec2, nints, skip):
    calls, overrides = official_spec2
    overrides["steps"]["photom"] = {"skip": skip}
    source = tmp_path / "jw_test_rateints.fits"
    _rateints(source, nints)
    result = run_spec2(source, tmp_path, tmp_path / "run.log", "jwst_test.pmap", overrides)
    assert calls == {"extract": 1, "photom": 0 if skip else 1}
    assert len(result.outputs) == 2
    with fits.open(result.outputs[1]) as hdus:
        assert hdus[0].header["S_PHOTOM"] == ("SKIPPED" if skip else "COMPLETE")
        flux = hdus["EXTRACT1D"].data["FLUX"]
        assert flux.size == nints * 3
        np.testing.assert_allclose(
            flux.reshape(nints, 3)[0], np.array([11, 12, 13]) * (1 if skip else 2)
        )
        assert hdus["EXTRACT1D"].columns["FLUX"].unit == ("DN/s" if skip else "Jy")
    assert ("saving uncalibrated SOSS" in result.log_path.read_text()) is skip


def test_parameters_preserve_overrides_and_existing_hooks():
    overrides = {"steps": {"photom": {"skip": True, "post_hooks": ["custom.hook"]}}}
    before = deepcopy(overrides)
    result = spec2_parameters(overrides)
    assert overrides == before
    assert result["steps"]["photom"]["post_hooks"] == ["custom.hook", SOSS_SAVE_HOOK]
    assert spec2_parameters(result) == result


@pytest.mark.parametrize("legacy", [False, True])
def test_soss_validation_supports_both_official_table_layouts(tmp_path, legacy):
    source = tmp_path / "test_rateints.fits"
    _rateints(source, 2)
    outputs = expected_stage2_outputs(source, tmp_path)
    _rateints(outputs[0], 2)
    with _spectrum(2, legacy=legacy) as model:
        model.meta.cal_step.photom = "SKIPPED"
        model.save(outputs[1])
    validate_soss_stage2(source, outputs, SKIP_PHOTOM)
    with fits.open(outputs[1], mode="update") as hdus:
        hdus[0].header["S_EXTR1D"] = "SKIPPED"
    with pytest.raises(PipelineExecutionError, match="not COMPLETE"):
        validate_soss_stage2(source, outputs, SKIP_PHOTOM)


@pytest.mark.parametrize("damage", ["missing", "empty", "not_fits", "missing_integrations"])
def test_invalid_spectra_are_rejected(tmp_path, damage):
    source = tmp_path / "test_rateints.fits"
    _rateints(source, 2)
    outputs = expected_stage2_outputs(source, tmp_path)
    _rateints(outputs[0], 2)
    if damage == "empty":
        outputs[1].touch()
    elif damage == "not_fits":
        outputs[1].write_bytes(b"not a spectrum")
    elif damage == "missing_integrations":
        with _spectrum(1) as model:
            model.meta.cal_step.photom = "SKIPPED"
            model.save(outputs[1])
    with pytest.raises(PipelineExecutionError, match="Invalid SOSS"):
        validate_soss_stage2(source, outputs, SKIP_PHOTOM)


def test_multisegment_missing_spectrum_resumes_without_stage1(
    tmp_path, exposure_records, official_spec2
):
    calls, overrides = official_spec2
    overrides["steps"]["photom"] = {"skip": True}
    config, discovery = _case(tmp_path, exposure_records, segments=(1, 2))
    config = replace(config, spec2_parameter_overrides=overrides)

    class Detector(FakeDetector1):
        def __call__(self, source, output_dir, parameters):
            super().__call__(source, output_dir, parameters)
            _rateints(expected_stage1_outputs(source, output_dir)[1], 2)

    detector = Detector()
    downloader = FakeDownloader()
    arguments = _arguments(discovery, downloader, detector, None)
    first = run_selected_through(config, **arguments)
    first.segments[0].stage2.outputs[1].unlink()
    second = run_selected_through(config, **arguments)
    assert len(detector.calls) == 2
    assert len(downloader.calls) == 2
    assert [s.stage1.status for s in second.segments] == ["skipped", "skipped"]
    assert [s.stage2.status for s in second.segments] == ["success", "skipped"]
    assert calls == {"extract": 3, "photom": 0}
    assert (
        SOSS_SAVE_HOOK
        in second.segments[0].stage2.manifest_entry["pipeline_configuration"][
            "effective_parameter_overrides"
        ]["steps"]["photom"]["post_hooks"]
    )


def test_historical_unsaved_extraction_failure_keeps_stage1(
    tmp_path, exposure_records, official_spec2, monkeypatch
):
    import json

    from jwst_redux.pipeline import soss

    calls, overrides = official_spec2
    overrides["steps"]["photom"] = {"skip": True}
    config, discovery = _case(tmp_path, exposure_records, segments=(1, 2))
    config = replace(config, spec2_parameter_overrides=overrides)

    class Detector(FakeDetector1):
        def __call__(self, source, output_dir, parameters):
            super().__call__(source, output_dir, parameters)
            _rateints(expected_stage1_outputs(source, output_dir)[1], 2)

    detector = Detector()
    downloader = FakeDownloader()
    arguments = _arguments(discovery, downloader, detector, None)
    # Reproduce issue #6 with the actual installed Spec2's original behavior.
    with monkeypatch.context() as original:
        original.setattr(soss, "spec2_parameters", deepcopy)
        with pytest.raises(PipelineExecutionError, match="Invalid SOSS"):
            run_selected_through(config, **arguments)
    workspace = Workspace.for_selection(config.discovery.output_root, config.selection)
    entries = json.loads(workspace.manifest.read_text())["runs"]
    assert [entry["status"] for entry in entries] == ["success", "failed"]
    assert len(list(workspace.stage2.glob("*_calints.fits"))) == 1
    assert not list(workspace.stage2.glob("*_x1dints.fits"))
    stage1_bytes = {path: path.read_bytes() for path in workspace.stage1.glob("*.fits")}

    result = run_selected_through(config, **arguments)
    assert [s.stage1.status for s in result.segments] == ["skipped", "success"]
    assert all(s.stage2.status == "success" for s in result.segments)
    assert all(path.read_bytes() == content for path, content in stage1_bytes.items())
    assert len(detector.calls) == len(downloader.calls) == 2
    assert calls == {"extract": 3, "photom": 0}


def test_save_hook_is_inert_for_bots(tmp_path):
    from jwst.photom import PhotomStep

    from jwst_redux.pipeline.soss import SaveUncalibratedSoss

    pipeline = Spec2Pipeline(output_dir=str(tmp_path), save_results=True)
    photom = PhotomStep(parent=pipeline, skip=True)
    hook = SaveUncalibratedSoss(parent=photom)
    with _spectrum(2) as model:
        model.meta.exposure.type = "NRS_BRIGHTOBJ"
        model.meta.cal_step.photom = "SKIPPED"
        assert hook.run(model) is model
    assert not list(tmp_path.glob("*.fits"))


def test_resume_revalidates_same_size_invalid_spectra(tmp_path, exposure_records, official_spec2):
    import json

    calls, overrides = official_spec2
    overrides["steps"]["photom"] = {"skip": True}
    config, discovery = _case(tmp_path, exposure_records, segments=(1,))
    config = replace(config, spec2_parameter_overrides=overrides)

    class Detector(FakeDetector1):
        def __call__(self, source, output_dir, parameters):
            super().__call__(source, output_dir, parameters)
            _rateints(expected_stage1_outputs(source, output_dir)[1])

    arguments = _arguments(discovery, FakeDownloader(), Detector(), None)
    first = run_selected_through(config, **arguments)
    product = first.segments[0].stage2.outputs[1]
    size = product.stat().st_size
    with fits.open(product, mode="update") as hdus:
        hdus[0].header["S_EXTR1D"] = "SKIPPED"
    assert product.stat().st_size == size
    with pytest.raises(PipelineExecutionError, match="not COMPLETE"):
        run_selected_through(config, **arguments)
    workspace = Workspace.for_selection(config.discovery.output_root, config.selection)
    entry = json.loads(workspace.manifest.read_text())["runs"][-1]
    assert entry["status"] == "failed"
    assert "cannot resume Spec2Pipeline" in Path(entry["log_path"]).read_text()
    assert calls == {"extract": 1, "photom": 0}
