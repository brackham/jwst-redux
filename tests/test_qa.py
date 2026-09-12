from __future__ import annotations

from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table
from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.config import (
    DEFAULT_SOSS_WAVELENGTH_WINDOWS,
    DiscoveryConfig,
    ExposureSelectionConfig,
    QueryConfig,
    WriteConfig,
)
from jwst_redux.provenance import ManifestStore
from jwst_redux.qa import soss
from jwst_redux.qa.common import (
    QA_SCHEMA_VERSION,
    SPECTROSCOPIC_TIME_SERIES_CONFIG,
    Spectrum,
    channel_classification_label,
    flux_to_f_lambda,
    point_to_point_difference_ppt,
    relative_flux_ppt,
    relative_scatter_ppt,
    robust_ppt_limit,
    spectra_from_x1dints,
    spectroscopic_time_series_display,
    spectroscopic_time_series_science_quality_mask,
    spectroscopic_time_series_validity_mask,
)
from jwst_redux.qa.soss import (
    POINT_TO_POINT_COLORBAR_LABEL,
    SPECTROSCOPIC_TIME_SERIES_COLORBAR_LABEL,
    select_spectroscopic_time_series_window,
    spectroscopic_time_series_colormap,
    spectroscopic_time_series_window,
)
from jwst_redux.qa.stage1 import generate as generate_stage1
from jwst_redux.qa.stage2 import generate as generate_stage2
from jwst_redux.qa.stage3 import generate as generate_stage3
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


def test_soss_qa_uses_spectroscopic_time_series_product_name(tmp_path: Path) -> None:
    path = tmp_path / "x1dints.fits"
    output = tmp_path / "qa"
    output.mkdir()
    _x1dints(path)

    generated = generate_stage2(path, output, DEFAULT_SOSS_WAVELENGTH_WINDOWS)

    assert {item.name for item in generated} == {
        "spectra.png",
        "white_light.png",
        "spectroscopic_time_series.png",
        "scatter_spectrum.png",
        "point_to_point_difference.png",
    }
    assert all(item.stat().st_size > 0 for item in generated)


def test_stage3_soss_qa_creates_scatter_and_point_to_point_products(tmp_path: Path) -> None:
    x1dints = tmp_path / "x1dints.fits"
    whtlt = tmp_path / "whtlt.ecsv"
    output = tmp_path / "qa"
    output.mkdir()
    _x1dints(x1dints)
    Table(
        {
            "MJD_UTC": [1.0, 1.1],
            "whitelight_flux_order_1": [10.0, 11.0],
            "whitelight_flux_order_2": [8.0, 9.0],
        }
    ).write(whtlt, format="ascii.ecsv")

    generated = generate_stage3(x1dints, whtlt, output, DEFAULT_SOSS_WAVELENGTH_WINDOWS)

    assert {item.name for item in generated} == {
        "spectra.png",
        "white_light.png",
        "spectroscopic_time_series.png",
        "scatter_spectrum.png",
        "point_to_point_difference.png",
    }
    assert all(item.stat().st_size > 0 for item in generated)


def test_white_light_columns_and_spectroscopic_time_series_normalization_are_discovered(
    tmp_path: Path,
) -> None:
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


def test_white_light_plot_has_combined_and_fixed_order_panels(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "x1dints.fits"
    _x1dints(path)
    groups, header, _, _ = spectra_from_x1dints(path)
    captured = []

    def capture(figure, output: Path) -> Path:
        captured.extend(figure.axes)
        return output

    monkeypatch.setattr(soss, "save_figure", capture)
    soss.white_light_proxy_plot(groups, soss.soss_title(header, "Stage 2"), tmp_path / "white.png")

    assert [axis.get_title() for axis in captured] == [
        soss.soss_title(header, "Stage 2"),
        "Order 1",
        "Order 2",
        "Order 3",
    ]
    assert [line.get_color() for line in captured[0].get_lines()[:2]] == ["C0", "C1"]
    assert captured[1].get_lines()[0].get_color() == "C0"
    assert captured[2].get_lines()[0].get_color() == "C1"
    assert len(captured[3].get_lines()) == 1  # Reference line only: no Order 3 data.


def test_f_lambda_conversion_and_spectroscopic_time_series_guardrails() -> None:
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
    residual, valid, science_quality, limit = spectroscopic_time_series_display(flux)
    assert valid.tolist() == [True, False, True, False, True]
    assert science_quality.tolist() == [True, False, True, False, False]
    assert np.isnan(residual[:, 1]).all()
    assert np.isnan(residual[:, 3]).all()
    assert np.isfinite(residual[:, 4]).all()
    assert limit == SPECTROSCOPIC_TIME_SERIES_CONFIG.scale_max_ppt
    assert channel_classification_label(valid, science_quality) == (
        "Valid channels shown: 3/5\nScience-quality channels: 2/5 (40%)"
    )

    small = np.array([[10.0], [10.001], [9.999]])
    _, _, _, small_limit = spectroscopic_time_series_display(small)
    assert small_limit == SPECTROSCOPIC_TIME_SERIES_CONFIG.scale_min_ppt
    assert SPECTROSCOPIC_TIME_SERIES_COLORBAR_LABEL == "Relative flux [ppt]"
    assert np.allclose(spectroscopic_time_series_colormap().get_bad()[:3], [0.82, 0.82, 0.82])


def test_spectroscopic_time_series_window_is_independent_of_quality_mask() -> None:
    wavelength = np.array([0.65, 0.70, 0.80, 0.90, 1.00])
    flux = np.array(
        [[10.0, 10.0, 0.1, 10.0, 10.0], [10.0, 10.0, 0.1, 10.0, 10.0]]
    )
    window = spectroscopic_time_series_window(3, DEFAULT_SOSS_WAVELENGTH_WINDOWS)
    selected_wavelength, selected_flux = select_spectroscopic_time_series_window(
        wavelength, flux, window
    )
    _, valid, science_quality, _ = spectroscopic_time_series_display(selected_flux)

    assert window == (0.70, 0.95)
    assert selected_wavelength.tolist() == [0.7, 0.8, 0.9]
    assert valid.tolist() == [True, False, True]
    assert science_quality.tolist() == [True, False, True]
    assert channel_classification_label(valid, science_quality) == (
        "Valid channels shown: 2/3\nScience-quality channels: 2/3 (67%)"
    )


def test_scatter_and_point_to_point_quantities_mask_invalid_channels_and_use_ppt() -> None:
    flux = np.array(
        [[10.0, 10.0, np.nan], [12.0, 8.0, np.nan], [14.0, 10.0, np.nan]]
    )
    valid = np.array([True, True, False])

    scatter = relative_scatter_ppt(flux, valid)
    differences = point_to_point_difference_ppt(flux, valid)

    assert np.allclose(scatter[:2], [1e3 * 1.4826 * 2 / 12, 0])
    assert np.isnan(scatter[2])
    assert differences.shape == (2, 3)
    assert np.allclose(differences[:, :2], [[1e3 * 2 / 12, -200], [1e3 * 2 / 12, 200]])
    assert np.isnan(differences[:, 2]).all()
    assert robust_ppt_limit(np.array([[1_000.0, -1_000.0]])) == 50.0
    assert POINT_TO_POINT_COLORBAR_LABEL == "Point-to-point relative difference [ppt]"


def test_soss_diagnostics_show_valid_but_science_quality_rejected_channels(
    monkeypatch, tmp_path: Path
) -> None:
    """High-scatter channels are diagnostic data, while invalid ones remain neutral."""
    wavelength = np.array([0.86, 0.90, 0.94])
    flux = np.array(
        [
            [10.0, 10.0, np.nan],
            [10.0, 20.0, np.nan],
            [10.0, 30.0, np.nan],
            [10.0, 10.0, np.nan],
            [10.0, 20.0, np.nan],
        ]
    )
    valid = spectroscopic_time_series_validity_mask(flux)
    science_quality = spectroscopic_time_series_science_quality_mask(flux)
    assert valid.tolist() == [True, True, False]
    assert science_quality.tolist() == [True, False, False]

    residual, displayed_valid, displayed_science_quality, limit = spectroscopic_time_series_display(
        flux
    )
    scatter = relative_scatter_ppt(flux, valid)
    differences = point_to_point_difference_ppt(flux, valid)
    assert displayed_valid.tolist() == valid.tolist()
    assert displayed_science_quality.tolist() == science_quality.tolist()
    assert np.isfinite(residual[:, 1]).all() and np.isnan(residual[:, 2]).all()
    assert scatter[1] > 250 and np.isnan(scatter[2])
    assert np.isfinite(differences[:, 1]).all() and np.isnan(differences[:, 2]).all()
    assert limit == SPECTROSCOPIC_TIME_SERIES_CONFIG.scale_max_ppt

    spectra = [
        Spectrum(1, index, 60000.0 + index / 1000, wavelength, row, None)
        for index, row in enumerate(flux, start=1)
    ]
    figures = []

    def capture(figure, output: Path) -> Path:
        figures.append(figure)
        return output

    monkeypatch.setattr(soss, "save_figure", capture)
    groups = {1: spectra}
    windows = {1: (0.85, 0.95)}
    soss.plot_scatter_spectrum(groups, "test", tmp_path / "scatter_spectrum.png", windows)
    soss.plot_point_to_point_difference(
        groups, "test", tmp_path / "point_to_point_difference.png", windows
    )
    soss.plot_spectroscopic_time_series(
        groups, "test", tmp_path / "spectroscopic_time_series.png", windows
    )

    scatter_values = figures[0].axes[0].lines[0].get_ydata()
    point_to_point_values = figures[1].axes[0].collections[0].get_array().filled(np.nan)
    time_series_values = figures[2].axes[0].collections[0].get_array().filled(np.nan)
    assert scatter_values[1] > 250 and np.isnan(scatter_values[2])
    assert np.any(np.abs(point_to_point_values) > 250) and np.isnan(point_to_point_values).any()
    assert np.any(np.abs(time_series_values) > 250) and np.isnan(time_series_values).any()


def test_spectra_plot_legend_labels_individual_integrations_and_median(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "x1dints.fits"
    _x1dints(path)
    groups, header, flux_unit, _ = spectra_from_x1dints(path)
    captured: list[tuple[list[str], int]] = []

    def capture(figure, output: Path) -> Path:
        for axis in figure.axes:
            legend = axis.get_legend()
            assert legend is not None
            captured.append(([text.get_text() for text in legend.get_texts()], legend._loc))
        return output

    monkeypatch.setattr(soss, "save_figure", capture)
    soss.spectra_plot(
        groups,
        soss.soss_title(header, "Stage 2"),
        flux_unit,
        tmp_path / "spectra.png",
        DEFAULT_SOSS_WAVELENGTH_WINDOWS,
    )

    assert captured
    assert all(labels == ["Individual integrations", "Temporal median"] for labels, _ in captured)
    assert all(location == 1 for _, location in captured)


def test_soss_qa_regenerates_new_outputs_from_local_pipeline_products_only(tmp_path: Path) -> None:
    config = _config(tmp_path / "work")
    workspace = Workspace.for_selection(config.discovery.output_root, config.selection)
    workspace.create()
    stage2_x1dints = workspace.stage2 / "local_x1dints.fits"
    stage3_x1dints = workspace.stage3 / "local_tso3_x1dints.fits"
    whtlt = workspace.stage3 / "local_tso3_whtlt.ecsv"
    _x1dints(stage2_x1dints)
    _x1dints(stage3_x1dints)
    Table(
        {
            "MJD_UTC": [1.0, 1.1],
            "whitelight_flux_order_1": [10.0, 11.0],
            "whitelight_flux_order_2": [8.0, 9.0],
        }
    ).write(whtlt, format="ascii.ecsv")
    manifest = ManifestStore(workspace.manifest, "TOI-3884")
    manifest.initialize()
    for run_id, operation, outputs in (
        ("stage2-local", "stage2", [_record(stage2_x1dints)]),
        ("stage3-local", "stage3", [_record(stage3_x1dints), _record(whtlt)]),
    ):
        manifest.append(
            {
                "run_id": run_id,
                "operation": operation,
                "status": "success",
                "exposure_identifier": config.selection.exposure_id,
                "segment_number": 1,
                "outputs": outputs,
            }
        )

    results = generate_qa(config, stages=("stage2", "stage3"))

    assert [result.stage for result in results] == ["stage2", "stage3"]
    for result in results:
        assert {path.name for path in result.outputs} == {
            "spectra.png",
            "white_light.png",
            "spectroscopic_time_series.png",
            "scatter_spectrum.png",
            "point_to_point_difference.png",
        }
    assert manifest.entry("stage2-local") is not None
    assert manifest.entry("stage2-local")["status"] == "success"
    assert manifest.entry("stage3-local") is not None
    assert manifest.entry("stage3-local")["status"] == "success"
    classification = results[0].manifest_entry["plotting_parameters"][
        "soss_channel_classification"
    ]
    assert "not the default display mask" in classification["science_quality"]


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
