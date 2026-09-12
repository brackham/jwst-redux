from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table
from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.config import DiscoveryConfig, ExposureSelectionConfig, QueryConfig, WriteConfig
from jwst_redux.provenance import ManifestStore
from jwst_redux.qa.common import (
    DYNAMIC_SPECTRUM_CONFIG,
    QA_SCHEMA_VERSION,
    dynamic_spectrum_display,
    flux_to_f_lambda,
    relative_flux_ppt,
    retained_channel_label,
    retained_wavelength_extent,
    spectra_from_x1dints,
)
from jwst_redux.qa.soss import DYNAMIC_COLORBAR_LABEL, dynamic_colormap
from jwst_redux.qa.stage1 import generate as generate_stage1
from jwst_redux.qa.stage3 import official_white_light
from jwst_redux.qa.workflow import QAResult, generate_qa
from jwst_redux.workspace import Workspace


def _config(root: Path) -> WriteConfig:
    return WriteConfig(
        discovery=DiscoveryConfig(
            QueryConfig("TOI-3884", "mast_targname", ("TOI-3884",), "NIRISS", "NIS_SOSS"),
            "uncal",
            root,
        ),
        selection=ExposureSelectionConfig("05799", "001", "001", "jw05799001001_04101_00001"),
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        tso3_parameter_overrides={},
        overwrite=False,
    )


def _rateints(path: Path) -> None:
    sci = np.array([[[1.0, 2.0], [3.0, np.nan]], [[2.0, 2.0], [3.0, 10.0]]])
    dq = np.zeros_like(sci, dtype=np.uint32)
    dq[1, 1, 1] = 1
    primary = fits.PrimaryHDU()
    primary.header["TARGNAME"] = "TOI-3884"
    primary.header["PROGRAM"] = "5799"
    fits.HDUList([primary, fits.ImageHDU(sci, name="SCI"), fits.ImageHDU(dq, name="DQ")]).writeto(
        path
    )


def _extract(order: int, start: int, rows: int = 2) -> fits.BinTableHDU:
    wavelength = np.tile(np.linspace(0.6 + order / 10, 0.8 + order / 10, 4), (rows, 1))
    flux = np.tile(np.arange(1, 5, dtype=float), (rows, 1)) * (1 + np.arange(rows)[:, None] / 100)
    dq = np.zeros((rows, 4), dtype=np.uint32)
    dq[0, 0] = 1
    columns = [
        fits.Column(name="INT_NUM", format="J", array=np.arange(start, start + rows)),
        fits.Column(name="SPORDER", format="J", array=np.full(rows, order)),
        fits.Column(
            name="TDB-MID", format="D", array=60000 + np.arange(start, start + rows) / 1000
        ),
        fits.Column(name="WAVELENGTH", format="4D", unit="um", array=wavelength),
        fits.Column(name="FLUX", format="4D", unit="Jy", array=flux),
        fits.Column(name="DQ", format="4J", array=dq),
    ]
    return fits.BinTableHDU.from_columns(columns, name="EXTRACT1D")


def _x1dints(path: Path, *, segmented: bool = False) -> None:
    hdus: list[fits.hdu.base.ExtensionHDU] = [_extract(1, 1), _extract(2, 1)]
    if segmented:
        hdus += [_extract(1, 3), _extract(2, 3), _extract(1, 5), _extract(2, 5)]
    primary = fits.PrimaryHDU()
    primary.header["TARGNAME"] = "TOI-3884"
    fits.HDUList([primary, *hdus]).writeto(path)


def _record(path: Path) -> dict[str, object]:
    return {"path": str(path), "size_bytes": path.stat().st_size}


def test_stage1_qa_uses_collision_safe_layout_and_masks_bad_pixels(tmp_path: Path) -> None:
    rateints = tmp_path / "same_rateints.fits"
    _rateints(rateints)
    output = tmp_path / "qa" / rateints.stem
    output.mkdir(parents=True)
    generated = generate_stage1(rateints, output)
    assert {path.name for path in generated} == {
        "median_detector.png",
        "flux_vs_integration.png",
        "temporal_scatter.png",
    }
    assert all(path.stat().st_size > 0 for path in generated)


def test_soss_parser_combines_all_segmented_extract_rows(tmp_path: Path) -> None:
    path = tmp_path / "combined_x1dints.fits"
    _x1dints(path, segmented=True)
    groups, _, unit, wavelength_unit = spectra_from_x1dints(path)
    assert unit == "Jy"
    assert wavelength_unit == "um"
    assert {order: len(spectra) for order, spectra in groups.items()} == {1: 6, 2: 6}
    assert [item.integration for item in groups[1]] == [1, 2, 3, 4, 5, 6]


def test_white_light_columns_and_dynamic_normalization_are_discovered(tmp_path: Path) -> None:
    path = tmp_path / "white.ecsv"
    Table(
        {
            "MJD_UTC": [1.0, 1.1],
            "BJD_TDB": [2.0, 2.1],
            "whitelight_flux_order_1": [10.0, 11.0],
            "whitelight_flux_order_3": [4.0, 5.0],
        }
    ).write(path, format="ascii.ecsv")
    curves = official_white_light(path)
    assert set(curves) == {1, 3}
    assert np.allclose(curves[1][0], [0, 2.4])
    ppt = relative_flux_ppt(np.array([[1.0, np.nan], [3.0, np.nan], [2.0, np.nan]]))
    assert np.allclose(ppt[:, 0], [-500, 500, 0])
    assert np.isnan(ppt[:, 1]).all()


def test_f_lambda_conversion_and_dynamic_display_guardrails() -> None:
    """F_nu conversion and display clipping preserve their physical definitions."""
    converted = flux_to_f_lambda(np.array([1.0]), np.array([1.0]), "Jy")
    assert np.allclose(converted, [2.99792458e-12], rtol=1e-7)

    flux = np.array(
        [
            [10.0, 0.1, 10.0, 10.0, 10.0],
            [10.01, 0.1, 11.0, np.nan, 20.0],
            [9.99, 0.1, 9.0, np.nan, 10.0],
        ]
    )
    residual, good, limit = dynamic_spectrum_display(flux)
    assert good.tolist() == [True, False, True, False, False]
    assert np.isnan(residual[:, 1]).all()
    assert np.isnan(residual[:, 3]).all()
    assert np.isnan(residual[:, 4]).all()
    assert limit == DYNAMIC_SPECTRUM_CONFIG.scale_max_ppt
    assert retained_wavelength_extent(
        np.arange(5, dtype=float), np.array([False, True, True, False, True])
    ) == (1.0, 2.0)
    assert retained_channel_label(good) == "Retained: 2/5 (40%)"

    small = np.array([[10.0], [10.001], [9.999]])
    _, _, small_limit = dynamic_spectrum_display(small)
    assert small_limit == DYNAMIC_SPECTRUM_CONFIG.scale_min_ppt
    assert DYNAMIC_COLORBAR_LABEL == "Relative flux [ppt]"
    assert np.allclose(dynamic_colormap().get_bad()[:3], [0.82, 0.82, 0.82])


def test_qa_provenance_failure_and_stale_rebuild_do_not_change_pipeline_status(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "work")
    workspace = Workspace.for_selection(config.discovery.output_root, config.selection)
    workspace.create()
    manifest = ManifestStore(workspace.manifest, "TOI-3884")
    manifest.initialize()
    good = workspace.stage1 / "good_rateints.fits"
    _rateints(good)
    manifest.append(
        {
            "run_id": "pipeline-good",
            "operation": "stage1",
            "status": "success",
            "exposure_identifier": config.selection.exposure_id,
            "segment_number": 1,
            "outputs": [_record(good)],
        }
    )
    first = generate_qa(config, stages=("stage1",))
    assert first[0].status == "success"
    assert first[0].manifest_entry["qa_schema_version"] == QA_SCHEMA_VERSION
    first[0].outputs[0].unlink()
    rebuilt = generate_qa(config, stages=("stage1",))
    assert rebuilt[0].status == "success"
    assert manifest.entry("pipeline-good")["status"] == "success"

    bad = workspace.stage1 / "bad_rateints.fits"
    bad.write_bytes(b"not fits")
    manifest.append(
        {
            "run_id": "pipeline-bad",
            "operation": "stage1",
            "status": "success",
            "exposure_identifier": config.selection.exposure_id,
            "segment_number": 2,
            "outputs": [_record(bad)],
        }
    )
    results = generate_qa(config, stages=("stage1",), force=True)
    failed = next(result for result in results if result.input_paths == (bad,))
    assert failed.status == "failed"
    pipeline = manifest.entry("pipeline-bad")
    assert pipeline is not None and pipeline["status"] == "success"
    assert pipeline["qa_status"] == "failed"


def test_cli_qa_regenerates_without_calling_pipeline(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path / "work")
    calls: list[tuple[str, ...]] = []

    def fake_generate(config_arg, *, stages, force):
        assert config_arg is config and force is False
        calls.append(stages)
        return (QAResult("stage1", "success", (), (tmp_path / "input.fits",), ("pipeline",), {}),)

    monkeypatch.setattr(cli, "load_write_config", lambda _: config)
    monkeypatch.setattr(cli, "generate_qa", fake_generate)
    result = CliRunner().invoke(cli.app, ["qa", "config.yaml", "--stage", "stage1"])
    assert result.exit_code == 0
    assert calls == [("stage1",)]
