"""Scientific masks, source isolation, and calibration-free fixed-slit QA reuse."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest
from astropy.io import fits
from fixedslit_fixtures import write_pair
from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.config import (
    BatchConfig,
    DiscoveryConfig,
    ExposureSelectionConfig,
    QueryConfig,
    write_config_for_exposure,
)
from jwst_redux.file_identity import checksum
from jwst_redux.provenance import ManifestStore
from jwst_redux.qa import fixedslit
from jwst_redux.qa.local import prepare_local_fixedslit_qa
from jwst_redux.qa.workflow import generate_qa
from jwst_redux.workspace import Workspace


def _header():
    return fits.Header(
        {
            "PROGRAM": "11441",
            "OBSERVTN": "007",
            "VISIT": "001",
            "TARGNAME": "",
            "TARGPROP": "TIC49871464",
            "INSTRUME": "NIRSPEC",
            "EXP_TYPE": "NRS_FIXEDSLIT",
            "DETECTOR": "MULTIPLE",
            "GRATING": "G235H",
            "FILTER": "F170LP",
            "FXD_SLIT": "S200A1",
        }
    )


def _setup(tmp_path, *, sources=1):
    query = QueryConfig(None, "mast_targname", (), "NIRSPEC", "NRS_FIXEDSLIT", ("11441",))
    batch = BatchConfig(
        DiscoveryConfig(query, "uncal", tmp_path),
        "planned",
        "auto",
        {},
        {},
        {},
        False,
        "continue",
        retention="final",
    )
    config = replace(
        write_config_for_exposure(
            batch, ExposureSelectionConfig("11441", "007", "001", "jw11441007001_03106_00001")
        ),
        group_id="test-group",
    )
    workspace = Workspace.for_config(config)
    workspace.create()
    manifest = ManifestStore(workspace.manifest, "TIC49871464")
    manifest.initialize()
    paths = write_pair(workspace.stage3 / "official_source", _header(), sources=sources)
    manifest.append(
        {
            "run_id": "calibration",
            "operation": "stage3",
            "status": "success",
            "group_id": config.group_id,
            "scientific_dataset": {
                "program_id": "11441",
                "observation_id": "007",
                "visit_number": "001",
            },
            "compatibility": {
                "program_id": "11441",
                "observation_id": "007",
                "visit_number": "001",
                "instrument": "NIRSPEC",
                "exposure_type": "NRS_FIXEDSLIT",
                "target_name": "TIC49871464",
                "grating": "G235H",
                "filter": "F170LP",
                "fixed_slit": "S200A1",
            },
            "exposure_identifiers": [config.selection.exposure_id],
            "association": {"members": [{"expname": "deleted_cal.fits", "exptype": "science"}]},
            "crds_context": "jwst_1584.pmap",
            "outputs": [manifest.file_record(p) for p in paths],
        }
    )
    return batch, config, workspace, manifest, paths


def test_all_tables_sources_masks_units_and_snr(tmp_path):
    paths = write_pair(tmp_path / "product", _header(), sources=2)
    spectra = fixedslit.read_x1d(paths[0])
    assert len(spectra) == 2
    for i, spectrum in enumerate(spectra):
        assert spectrum.header["SOURCEID"] == i + 1
        assert spectrum.flux_unit == "Jy"
        assert spectrum.wavelength_unit == "um"
        assert not spectrum.valid[10] and not spectrum.valid[15]
        assert not np.any(spectrum.valid[30:40])
        assert np.isnan(spectrum.snr[11:14]).all()
        assert spectrum.snr[0] == pytest.approx(spectrum.flux[0] / 0.001)
        assert np.isfinite(spectrum.flux[-1])  # no outlier rejection in science
    images = fixedslit.read_s2d(paths[1])
    assert len(images) == 2
    assert np.isnan(images[0].data[:, 30:40]).all()
    assert images[0].detectors == ["NRS1", "NRS2"]
    assert images[0].unit == "MJy"
    np.testing.assert_allclose(images[0].wavelength, spectra[0].wavelength)
    text = fixedslit.title(spectra[0].header)
    for value in ("11441", "007", "TIC49871464", "G235H/F170LP", "S200A1", "POINT"):
        assert value in text


def test_vector_rows_remain_separate_spectra(tmp_path):
    paths = write_pair(tmp_path / "vectors", _header(), sources=2, vector_rows=True)
    spectra = fixedslit.read_x1d(paths[0])
    assert len(spectra) == 4
    assert all(s.flux.shape == (100,) for s in spectra)
    assert [s.header["SOURCEID"] for s in spectra] == [1, 1, 2, 2]


def test_plots_do_not_bridge_masked_or_coordinate_gaps_and_keep_robust_limits(
    monkeypatch, tmp_path
):
    paths = write_pair(tmp_path / "product", _header())
    spectra = fixedslit.read_x1d(paths[0])
    saved = []
    monkeypatch.setattr(fixedslit, "save_figure", lambda fig, p: saved.append(fig) or p)
    fixedslit._line_plot(spectra, tmp_path / "spectra.png", snr=False)
    axis = saved[0].axes[0]
    assert axis.get_ylim()[1] < 1
    for line in axis.lines:
        x = line.get_xdata()
        assert not (np.min(x) < 1.3 and np.max(x) >= 1.4)
    wave = np.array([1.0, 1.01, 1.02, 2.0, 2.01])
    runs = fixedslit._runs(wave, np.ones(5, bool))
    assert [r.tolist() for r in runs] == [[0, 1, 2], [3, 4]]
    for fig in saved:
        plt.close(fig)
    low, high = fixedslit._display_limits(np.full(10, 1e-20))
    assert 0 < low < 1e-20 < high < 2e-20


@pytest.mark.parametrize("error_case", ["missing", "wrong_unit", "negative", "nan"])
def test_bad_uncertainties_reported_without_changing_flux(tmp_path, error_case):
    x1d, s2d = write_pair(tmp_path / "product", _header())
    with fits.open(x1d) as hdul:
        primary, table = hdul[0].copy(), hdul[1].copy()
    if error_case == "missing":
        columns = fits.ColDefs([c for c in table.columns if c.name != "FLUX_ERROR"])
        table = fits.BinTableHDU.from_columns(columns, header=table.header, name="EXTRACT1D")
    elif error_case == "wrong_unit":
        table.columns["FLUX_ERROR"].unit = "s"
    else:
        table.data["FLUX_ERROR"][:] = -1 if error_case == "negative" else np.nan
    fits.HDUList([primary, table]).writeto(x1d, overwrite=True)
    before = checksum(x1d)
    outputs = fixedslit.generate(x1d, s2d, tmp_path / "qa")
    report = json.loads(outputs[-1].read_text())
    assert report["checks"]["usable_snr"] is False
    assert report["spectra"][0]["warnings"]
    assert report["spectra"][0]["snr"] is None
    assert before == checksum(x1d)


def test_unit_conversion_and_malformed_error_shape(tmp_path):
    x1d, _ = write_pair(tmp_path / "product", _header())
    with fits.open(x1d) as hdul:
        primary, table = hdul[0].copy(), hdul[1].copy()
    table.data["WAVELENGTH"] *= 1000
    table.columns["WAVELENGTH"].unit = "nm"
    table.data["FLUX_ERROR"] *= 1000
    table.columns["FLUX_ERROR"].unit = "mJy"
    fits.HDUList([primary, table]).writeto(x1d, overwrite=True)
    spectrum = fixedslit.read_x1d(x1d)[0]
    assert spectrum.wavelength[0] == 1
    assert spectrum.error[0] == pytest.approx(0.001)
    columns = [c for c in table.columns if c.name != "FLUX_ERROR"]
    columns.append(fits.Column(name="FLUX_ERROR", format="2D", unit="Jy", array=np.ones((100, 2))))
    malformed = fits.BinTableHDU.from_columns(columns, header=table.header, name="EXTRACT1D")
    fits.HDUList([primary, malformed]).writeto(x1d, overwrite=True)
    spectrum = fixedslit.read_x1d(x1d)[0]
    assert np.isnan(spectrum.error).all()
    assert "shape" in " ".join(spectrum.warnings)


def test_fully_flagged_extraction_is_visible_quality_warning_with_image(tmp_path):
    x1d, s2d = write_pair(tmp_path / "bad", _header())
    with fits.open(x1d, mode="update") as hdul:
        hdul[1].data["DQ"][:] = 1
    outputs = fixedslit.generate(x1d, s2d, tmp_path / "qa")
    assert len(outputs) == 4
    report = json.loads((tmp_path / "qa/checks.json").read_text())
    assert not report["checks"]["usable_extracted_flux_and_image"]
    assert report["spectra"][0]["invalid_or_flagged_fraction"] == 1
    assert report["quality_status"] == "unusable"


def test_pixel_fallback_is_explicit_and_bad_s2d_masks_fail(tmp_path):
    x1d, s2d = write_pair(tmp_path / "product", _header())
    with fits.open(s2d, mode="update") as hdul:
        hdul["WAVELENGTH"].data[0, 0] += 1
    image = fixedslit.read_s2d(s2d)[0]
    assert image.wavelength is None
    assert "resampled pixels" in image.warnings[0]
    with fits.open(s2d, mode="update") as hdul:
        hdul["WHT"].data = np.zeros((2, 2))
    with pytest.raises(ValueError, match="Unusable"):
        fixedslit.generate(x1d, s2d, tmp_path / "qa")
    assert "malformed WHT" in (tmp_path / "qa/checks.json").read_text()


def test_stage3_only_resume_force_integrity_and_historical_preservation(tmp_path):
    _, config, workspace, manifest, paths = _setup(tmp_path, sources=2)
    historical = manifest.entries()
    hashes = {str(p): checksum(p) for p in paths}
    assert not list(workspace.stage1.glob("*.fits"))
    assert not list(workspace.stage2.glob("*.fits"))
    first = generate_qa(config, stages=("stage3",))[0]
    assert first.status == "success"
    assert {p.name for p in first.outputs} == {
        "spectra.png",
        "snr_spectrum.png",
        "spectrum_2d.png",
        "checks.json",
    }
    assert len(json.loads(first.outputs[-1].read_text())["spectra"]) == 2
    for p in (*paths, *first.outputs):
        details = p.stat()
        os.utime(p, ns=(details.st_atime_ns, details.st_mtime_ns + 10))
    assert generate_qa(config, stages=("stage3",))[0].status == "skipped"
    assert generate_qa(config, stages=("stage3",), force=True)[0].status == "success"
    first.outputs[0].unlink()
    assert generate_qa(config, stages=("stage3",))[0].status == "success"
    # Same-size corruption of a plot is detected even though calibration is intact.
    p = first.outputs[0]
    p.write_bytes(b"!" + p.read_bytes()[1:])
    assert generate_qa(config, stages=("stage3",))[0].status == "success"
    assert manifest.entries()[: len(historical)] == historical
    assert {str(p): checksum(p) for p in paths} == hashes
    assert all(r["sha256"] for r in first.manifest_entry["input_pipeline_products"])
    assert first.manifest_entry["upstream_successful_run_ids"] == ["calibration"]


@pytest.mark.parametrize("damage", ["missing", "same_size", "missing_record"])
def test_missing_or_corrupt_stage3_input_is_failed_qa_not_calibration(tmp_path, damage):
    _, config, _, manifest, paths = _setup(tmp_path)
    if damage == "missing":
        paths[1].unlink()
    elif damage == "missing_record":
        manifest.update("calibration", {"outputs": [manifest.file_record(paths[0])]})
    else:
        paths[1].write_bytes(b"!" + paths[1].read_bytes()[1:])
    historical = manifest.entries()
    result = generate_qa(config, stages=("stage3",))[0]
    assert result.status == "failed"
    assert "Missing or changed" in result.manifest_entry["error"]
    assert result.manifest_entry["diagnostic_report"]
    assert manifest.entries()[: len(historical)] == historical


def test_batch_cli_is_local_only_and_selected_group_resolution(monkeypatch, tmp_path):
    batch, config, workspace, manifest, _ = _setup(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("QA attempted archive discovery or calibration")

    monkeypatch.setattr(cli, "prepare_batch", forbidden)
    monkeypatch.setattr(cli, "run_batch", forbidden)
    monkeypatch.setattr(cli, "run_selected_through", forbidden)
    monkeypatch.setattr(cli, "load_batch_config", lambda _: batch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3"])
    assert result.exit_code == 0, result.output
    assert "success" in result.output
    assert generate_qa(replace(config, group_id=None), stages=("stage3",))[0].status == "skipped"
    again = runner.invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3"])
    assert again.exit_code == 0 and "skipped" in again.output
    forced = runner.invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3", "--force"])
    assert forced.exit_code == 0 and "success" in forced.output
    assert not list(workspace.stage2.glob("*.fits"))
    outputs = manifest.entry("calibration")["outputs"]
    Path(outputs[1]["path"]).unlink()
    failed = runner.invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3"])
    assert failed.exit_code == 1 and "failed" in failed.output


@pytest.mark.parametrize(
    "key,value",
    [
        ("proposal_ids", ("1128",)),
        ("observation_id", "008"),
        ("visit_id", "11441007002"),
        ("target", "other"),
        ("grating", "G140H"),
        ("filter", "F290LP"),
    ],
)
def test_local_batch_query_filters(tmp_path, key, value):
    batch, _, _, _, _ = _setup(tmp_path)
    query = replace(batch.discovery.query, **{key: value})
    from jwst_redux.exceptions import QAUnavailableError

    with pytest.raises(QAUnavailableError, match="No recorded"):
        prepare_local_fixedslit_qa(replace(batch, discovery=replace(batch.discovery, query=query)))


def test_source_pair_mismatch_fails_with_report(tmp_path):
    x1d, s2d = write_pair(tmp_path / "pair", _header())
    with fits.open(s2d, mode="update") as hdul:
        hdul["SCI"].header["SOURCEID"] = 99
    with pytest.raises(ValueError, match="Unusable"):
        fixedslit.generate(x1d, s2d, tmp_path / "qa")
    assert "identifiers disagree" in (tmp_path / "qa/checks.json").read_text()


def test_official_datamodel_s2d_uses_declared_gwcs_units(tmp_path):
    from astropy import units as u
    from astropy.modeling import models
    from gwcs import WCS
    from gwcs import coordinate_frames as cf
    from jwst import datamodels

    frame = cf.CompositeFrame(
        [
            cf.Frame2D(name="sky", axes_order=(0, 1), unit=(u.deg, u.deg)),
            cf.SpectralFrame(name="spectral", axes_order=(2,), unit=u.nm),
        ],
        name="world",
    )
    transform = models.Mapping((0, 1, 0)) | (
        models.Scale(0.01) & models.Scale(0.01) & (models.Scale(10) | models.Shift(1000))
    )
    wcs = WCS([(cf.Frame2D(name="detector"), transform), (frame, None)])
    model = datamodels.SlitModel(data=np.ones((5, 100), np.float32))
    model.meta.wcs = wcs
    model.meta.wcsinfo.dispersion_direction = 1
    model.meta.bunit_data = "MJy"
    path = tmp_path / "official_s2d.fits"
    model.save(path)
    model.close()
    image = fixedslit.read_s2d(path)[0]
    np.testing.assert_allclose(image.wavelength, 1 + np.arange(100) * 0.01)
    assert not any("wavelength axis unavailable" in w.lower() for w in image.warnings)


def test_multiple_product_pairs_are_independent_and_partial_failure_preserves_siblings(tmp_path):
    _, config, workspace, manifest, paths = _setup(tmp_path)
    extra = write_pair(workspace.stage3 / "other_slit", _header())
    calibration = manifest.entry("calibration")
    manifest.update(
        "calibration",
        {"outputs": calibration["outputs"] + [manifest.file_record(p) for p in extra]},
    )
    first = generate_qa(config, stages=("stage3",))
    assert len(first) == 2 and all(r.status == "success" for r in first)
    assert first[0].outputs[0].parent != first[1].outputs[0].parent
    paths[1].unlink()
    second = generate_qa(config, stages=("stage3",))
    assert sorted(r.status for r in second) == ["failed", "skipped"]


def test_separate_detector_segments_and_nonfinite_wavelengths(tmp_path):
    x1d, _ = write_pair(tmp_path / "detectors", _header(), sources=2)
    with fits.open(x1d, mode="update") as hdul:
        for i in (1, 2):
            hdu = hdul["EXTRACT1D", i]
            hdu.header["DETECTOR"] = f"NRS{i}"
            hdu.header["SOURCEID"] = 1
            hdu.header["SLTNAME"] = "S200A1"
        hdul["EXTRACT1D", 1].data["WAVELENGTH"][:3] = [np.nan, 0, -1]
        hdul["EXTRACT1D", 2].data["WAVELENGTH"] += 2
    spectra = fixedslit.read_x1d(x1d)
    assert [s.header["DETECTOR"] for s in spectra] == ["NRS1", "NRS2"]
    assert not np.any(spectra[0].valid[:3])
    assert spectra[1].wavelength.min() > spectra[0].wavelength[3:].max()
    assert all(s.header["SOURCEID"] == 1 for s in spectra)
    # The two detectors are independent panels even when source IDs are the same.
    assert fixedslit._summary(spectra[0])["counts"]["invalid_or_flagged"] == 15


def test_s2d_vertical_dispersion_is_transposed_without_relabelling_pixels(tmp_path):
    _, s2d = write_pair(tmp_path / "vertical", _header())
    original = fixedslit.read_s2d(s2d)[0]
    with fits.open(s2d, mode="update") as hdul:
        hdul["SCI"].header["DISPAXIS"] = 2
        for name in ("SCI", "WHT", "WAVELENGTH"):
            hdul[name].data = hdul[name].data.T
    image = fixedslit.read_s2d(s2d)[0]
    np.testing.assert_allclose(image.data, original.data, equal_nan=True)
    np.testing.assert_allclose(image.wavelength, original.wavelength)


def test_corrupt_fits_with_recorded_identity_fails_and_writes_report(tmp_path):
    _, config, _, manifest, paths = _setup(tmp_path)
    paths[0].write_bytes(b"not a FITS product")
    manifest.update("calibration", {"outputs": [manifest.file_record(p) for p in paths]})
    result = generate_qa(config, stages=("stage3",))[0]
    assert result.status == "failed"
    assert "Unusable fixed-slit" in result.manifest_entry["error"]
    assert result.manifest_entry["diagnostic_report"]


def test_unusable_extraction_warning_is_recorded_printed_and_resumable(monkeypatch, tmp_path):
    batch, config, _, manifest, paths = _setup(tmp_path)
    with fits.open(paths[0], mode="update") as hdul:
        hdul["EXTRACT1D"].data["DQ"][:] = 1
    manifest.update("calibration", {"outputs": [manifest.file_record(p) for p in paths]})
    result = generate_qa(config, stages=("stage3",))[0]
    assert result.status == "success" and result.manifest_entry["quality_status"] == "unusable"
    assert result.manifest_entry["qa_warnings"]
    monkeypatch.setattr(cli, "load_batch_config", lambda _: batch)
    repeated = CliRunner().invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3"])
    assert repeated.exit_code == 0
    assert "skipped" in repeated.output and "QA quality warning" in repeated.output


def test_narrow_local_query_does_not_hydrate_unrelated_canonical_cloud_group(tmp_path):
    batch, _, _, _, _ = _setup(tmp_path)
    unrelated = tmp_path / "groups/nirspec-nrs_fixedslit-11441-113-001-0123456789abcdef"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text("not readable JSON")
    narrowed = replace(
        batch,
        discovery=replace(
            batch.discovery, query=replace(batch.discovery.query, observation_id="007")
        ),
    )
    selected = prepare_local_fixedslit_qa(narrowed)
    assert len(selected.branches) == 1  # noncanonical legacy names remain supported


def test_qa_writes_replace_cloud_outputs_without_opening_existing_files_for_write(
    monkeypatch, tmp_path
):
    from jwst_redux.qa.checks import write_report
    from jwst_redux.qa.common import save_figure

    png, report = tmp_path / "plot.png", tmp_path / "checks.json"
    png.write_bytes(b"prior plot")
    report.write_text("prior report")
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if path in {png, report} and "w" in mode:
            pytest.fail("overwriting existing cloud output can require hydration")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    figure, axis = plt.subplots()
    axis.plot([0, 1], [0, 1])
    save_figure(figure, png)
    write_report({"result": "warning"}, report)
    assert png.read_bytes().startswith(b"\x89PNG")
    assert json.loads(report.read_text()) == {"result": "warning"}
    assert set(tmp_path.iterdir()) == {png, report}


def test_failed_qa_render_keeps_previous_output_and_closes_figure(monkeypatch, tmp_path):
    from jwst_redux.qa.common import save_figure

    path = tmp_path / "prior.png"
    path.write_bytes(b"prior plot")
    figure, _ = plt.subplots()

    def fail(*args, **kwargs):
        raise OSError("synthetic render failure")

    monkeypatch.setattr(figure, "savefig", fail)
    with pytest.raises(OSError, match="synthetic render"):
        save_figure(figure, path)
    assert path.read_bytes() == b"prior plot"
    assert not plt.fignum_exists(figure.number)
    assert list(tmp_path.iterdir()) == [path]
