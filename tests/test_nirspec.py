"""Focused NIRSpec/BOTS discovery, planning, association, and QA tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

from jwst_redux.batch import prepare_batch, run_batch
from jwst_redux.config import BatchConfig, DiscoveryConfig, QueryConfig
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import ArchiveQueryError, PlanningError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import DiscoveryResult, discover, normalize_exposure
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.pipeline.stages import pipeline_path_for
from jwst_redux.planning.resolver import make_reduction_plans
from jwst_redux.provenance import ManifestStore
from jwst_redux.qa import nirspec
from jwst_redux.qa.checks import extracted_spectrum_report
from jwst_redux.qa.common import F_LAMBDA_LABEL
from jwst_redux.qa.workflow import generate_qa
from jwst_redux.workspace import Workspace


def _record(*, grating: str, detector: str = "NRS1") -> dict:
    return {
        "ArchiveFileID": 1,
        "fileSetName": "jw05863002001_03101_00001",
        "productLevel": "1b",
        "program": 5863,
        "observtn": 2,
        "visit": 1,
        "visit_id": "05863002001",
        "targname": "TOI-3884b",
        "instrume": "NIRSPEC",
        "exp_type": "NRS_BRIGHTOBJ",
        "tsovisit": "t",
        "opticalElements": f"F290LP;{grating}",
        "filter": "F290LP",
        "detector": detector,
        "subarray": "SUB2048",
        "nints": 10,
        "ngroups": 3,
        "exsegtot": 2,
        "access": "PUBLIC",
        "proposal_type": "GO",
    }


def _products(grating: str) -> tuple:
    exposure_id = "jw05863002001_03101_00001"
    records = []
    for detector in ("nrs1", "nrs2"):
        for segment in (2, 1):
            filename = f"{exposure_id}-seg{segment:03d}_{detector}_uncal.fits"
            records.append(
                {
                    "dataset": exposure_id,
                    "filename": filename,
                    "uri": f"{exposure_id}/{filename}",
                    "file_suffix": "_uncal",
                    "category": "1b",
                    "size": 2880,
                    "type": "science",
                    "filters": f"F290LP;{grating}",
                    "access": "PUBLIC",
                }
            )
    return select_starting_products(normalize_products(records), "uncal")


def _discovery(grating: str) -> DiscoveryResult:
    exposure = normalize_exposure(_record(grating=grating))
    attached = attach_products((exposure,), _products(grating))
    return DiscoveryResult(build_science_datasets(attached))


def _discovery_config(root: Path, grating: str) -> DiscoveryConfig:
    return DiscoveryConfig(
        QueryConfig(
            "TOI-3884",
            "mast_targname",
            ("TOI-3884", "TOI-3884b"),
            "NIRSPEC",
            "NRS_BRIGHTOBJ",
        ),
        "uncal",
        root / grating.lower(),
    )


def _batch_config(root: Path, grating: str) -> BatchConfig:
    return BatchConfig(
        discovery=_discovery_config(root, grating),
        endpoint="planned",
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        tso3_parameter_overrides={},
        overwrite=False,
        failure_policy="continue",
    )


def test_nirspec_bots_exposure_and_product_metadata_are_normalized() -> None:
    exposure = normalize_exposure(_record(grating="G395M"))
    products = _products("G395M")

    assert (exposure.instrument, exposure.exposure_type, exposure.is_tso) == (
        "NIRSPEC",
        "NRS_BRIGHTOBJ",
        True,
    )
    assert (exposure.grating, exposure.filter, exposure.subarray) == (
        "G395M",
        "F290LP",
        "SUB2048",
    )
    assert {product.detector for product in products} == {"NRS1", "NRS2"}
    assert pipeline_path_for(exposure).classes == (
        "Detector1Pipeline",
        "Spec2Pipeline",
        "Tso3Pipeline",
    )


def test_nirspec_acquisition_is_not_a_supported_science_exposure() -> None:
    acquisition = replace(
        normalize_exposure(_record(grating="MIRROR")),
        exposure_type="NRS_WATA",
    )
    with pytest.raises(PlanningError, match="Only NIRISS/NIS_SOSS and NIRSpec/NRS_BRIGHTOBJ"):
        pipeline_path_for(acquisition)
    with pytest.raises(ArchiveQueryError, match="No scientific dataset identity rule"):
        build_science_datasets((acquisition,))


def test_discovery_defensively_separates_nirspec_acquisition_rows(tmp_path: Path) -> None:
    science = _record(grating="G395M")
    acquisition = {
        **science,
        "ArchiveFileID": 2,
        "fileSetName": "jw05863002001_02101_00001",
        "exp_type": "NRS_WATA",
        "tsovisit": "f",
        "opticalElements": "F140X;MIRROR",
        "filter": "F140X",
        "exsegtot": 1,
    }

    class Client:
        def query_exposures(self, criteria, select_columns):
            return [acquisition, science]

        def list_products(self, exposure_ids):
            assert exposure_ids == [science["fileSetName"]]
            return [product.metadata for product in _products("G395M")]

    result = discover(_discovery_config(tmp_path, "G395M"), client=Client())

    assert len(result.exposures) == 1
    assert result.exposures[0].exposure_type == "NRS_BRIGHTOBJ"


def test_g395m_plans_only_the_nrs1_science_branch(tmp_path: Path) -> None:
    discovery = _discovery("G395M")
    plans = make_reduction_plans(_discovery_config(tmp_path, "G395M"), discovery.datasets)

    assert len(plans) == 1
    assert len(plans[0].reductions) == 1
    reduction = plans[0].reductions[0]
    assert dict(reduction.group.compatibility)["detector"] == "NRS1"
    assert {product.detector for product in reduction.archive_products} == {"NRS1"}
    assert len(reduction.archive_products) == 2
    assert [stage.name for stage in reduction.stages] == [
        "Detector1Pipeline",
        "Spec2Pipeline",
        "Tso3Pipeline",
    ]


def test_g395h_plans_detector_aware_nrs1_and_nrs2_branches(tmp_path: Path) -> None:
    discovery = _discovery("G395H")
    plans = make_reduction_plans(_discovery_config(tmp_path, "G395H"), discovery.datasets)

    reductions = plans[0].reductions
    assert [dict(item.group.compatibility)["detector"] for item in reductions] == [
        "NRS1",
        "NRS2",
    ]
    assert all(len(item.archive_products) == 2 for item in reductions)
    assert all(
        {product.detector for product in item.archive_products}
        == {dict(item.group.compatibility)["detector"]}
        for item in reductions
    )
    assert all(dict(item.group.compatibility)["grating"] == "G395H" for item in reductions)


class _Downloader:
    def download_product(self, uri: str, destination: Path):
        detector = "NRS2" if "_nrs2_" in destination.name else "NRS1"
        primary = fits.PrimaryHDU()
        for key, value in {
            "INSTRUME": "NIRSPEC",
            "EXP_TYPE": "NRS_BRIGHTOBJ",
            "DETECTOR": detector,
            "GRATING": "G395H",
            "FILTER": "F290LP",
            "SUBARRAY": "SUB2048",
        }.items():
            primary.header[key] = value
        primary.writeto(destination)
        return "COMPLETE", None, None


class _Detector1:
    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        for path in expected_stage1_outputs(input_path, output_dir):
            path.write_bytes(b"stage1")


class _Spec2:
    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        detector = "NRS2" if "_nrs2_" in input_path.name else "NRS1"
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        primary = fits.PrimaryHDU()
        for key, value in {
            "INSTRUME": "NIRSPEC",
            "EXP_TYPE": "NRS_BRIGHTOBJ",
            "DETECTOR": detector,
            "GRATING": "G395H",
            "FILTER": "F290LP",
            "SUBARRAY": "SUB2048",
        }.items():
            primary.header[key] = value
        primary.writeto(calints)
        x1dints.write_bytes(b"stage2")


class _Tso3:
    def __init__(self) -> None:
        self.members_by_product: dict[str, list[str]] = {}

    def __call__(self, association_path: Path, output_dir: Path, overrides: dict) -> None:
        association = json.loads(association_path.read_text(encoding="utf-8"))
        product = association["products"][0]
        self.members_by_product[product["name"]] = [
            Path(member["expname"]).name for member in product["members"]
        ]
        (output_dir / f"{product['name']}_x1dints.fits").write_bytes(b"x1dints")
        (output_dir / f"{product['name']}_whtlt.ecsv").write_bytes(b"whtlt")


def test_g395h_stage3_associations_and_workspaces_remain_detector_separate(
    tmp_path: Path,
) -> None:
    discovery = _discovery("G395H")
    config = _batch_config(tmp_path, "G395H")
    prepared = prepare_batch(config, discoverer=lambda _: discovery)
    tso3 = _Tso3()

    result = run_batch(
        config,
        prepared=prepared,
        downloader=_Downloader(),
        detector1_pipeline=_Detector1(),
        spec2_pipeline=_Spec2(),
        tso3_pipeline=tso3,
        context_resolver=lambda _: "jwst_test.pmap",
    )

    assert not result.failures
    assert {branch.config.selection.detector for branch in prepared.branches} == {
        "NRS1",
        "NRS2",
    }
    assert set(tso3.members_by_product) == {
        "jw05863002001_03101_00001_nrs1_tso3",
        "jw05863002001_03101_00001_nrs2_tso3",
    }
    for product_name, members in tso3.members_by_product.items():
        detector = product_name.split("_")[-2]
        assert len(members) == 2
        assert all(f"_{detector}_calints.fits" in member for member in members)
    roots = {
        Workspace.for_selection(config.discovery.output_root, branch.config.selection).root
        for branch in prepared.branches
    }
    assert len(roots) == 2


def test_g395h_final_retention_prunes_each_detector_branch(tmp_path: Path) -> None:
    discovery = _discovery("G395H")
    config = replace(_batch_config(tmp_path, "G395H"), retention="final")
    prepared = prepare_batch(config, discoverer=lambda _: discovery)

    result = run_batch(
        config,
        prepared=prepared,
        downloader=_Downloader(),
        detector1_pipeline=_Detector1(),
        spec2_pipeline=_Spec2(),
        tso3_pipeline=_Tso3(),
        context_resolver=lambda _: "jwst_test.pmap",
    )

    assert not result.failures
    for item in result.branches:
        assert item.workflow is not None
        assert item.workflow.retention is not None
        assert item.workflow.retention.status == "success"
        workspace = Workspace.for_selection(
            config.discovery.output_root, item.branch.config.selection
        )
        assert not any(workspace.stage1.glob("*.fits"))
        assert not any(workspace.stage2.glob("*.fits"))
        assert any(workspace.stage3.glob("*.fits"))
        assert item.workflow.stage3 is not None
        assert item.workflow.stage3.association.path.is_file()


def _nirspec_x1dints(path: Path) -> None:
    primary = fits.PrimaryHDU()
    for key, value in {
        "INSTRUME": "NIRSPEC",
        "EXP_TYPE": "NRS_BRIGHTOBJ",
        "DETECTOR": "NRS2",
        "GRATING": "G395H",
        "FILTER": "F290LP",
        "SUBARRAY": "SUB2048",
        "NINTS": 2,
    }.items():
        primary.header[key] = value
    wavelength = np.tile(np.linspace(3.82, 5.18, 4), (2, 1))
    flux = np.tile(np.arange(1, 5, dtype=float), (2, 1))
    table = fits.BinTableHDU.from_columns(
        [
            fits.Column(name="INT_NUM", format="J", array=[1, 2]),
            fits.Column(name="WAVELENGTH", format="4D", unit="um", array=wavelength),
            fits.Column(name="FLUX", format="4D", unit="Jy", array=flux),
            fits.Column(name="DQ", format="4J", array=np.zeros((2, 4), dtype=np.uint32)),
        ],
        name="EXTRACT1D",
    )
    fits.HDUList([primary, table]).writeto(path)


def test_nirspec_qa_dispatch_and_detector_coverage_report(monkeypatch, tmp_path: Path) -> None:
    config = _batch_config(tmp_path, "G395H")
    prepared = prepare_batch(config, discoverer=lambda _: _discovery("G395H"))
    branch = next(item for item in prepared.branches if item.config.selection.detector == "NRS2")
    workspace = Workspace.for_selection(config.discovery.output_root, branch.config.selection)
    workspace.create()
    path = workspace.stage2 / "synthetic_nrs2_x1dints.fits"
    _nirspec_x1dints(path)
    manifest = ManifestStore(workspace.manifest, "TOI-3884")
    manifest.initialize()
    manifest.append(
        {
            "run_id": "nrs-stage2",
            "operation": "stage2",
            "status": "success",
            "selection": {
                "program_id": branch.config.selection.program_id,
                "observation_id": branch.config.selection.observation_id,
                "visit_number": branch.config.selection.visit_number,
                "exposure_number": branch.config.selection.resolved_exposure_number,
                "exposure_id": branch.config.selection.exposure_id,
                "label": branch.config.selection.label,
                "workspace_name": branch.config.selection.workspace_name,
                "detector": "NRS2",
            },
            "exposure_identifier": branch.config.selection.exposure_id,
            "segment_number": 1,
            "outputs": [{"path": str(path), "size_bytes": path.stat().st_size}],
        }
    )
    called: list[str] = []

    def fake_generate(
        path_arg,
        output_dir,
        *,
        stage,
        white_light_path=None,
        quicklook_cadence_minutes=2.0,
    ):
        assert quicklook_cadence_minutes == 2.0
        called.append(stage)
        output = output_dir / "nirspec.png"
        output.write_bytes(b"png")
        return (output,)

    monkeypatch.setattr("jwst_redux.qa.workflow.nirspec_qa.generate", fake_generate)
    results = generate_qa(branch.config, stages=("stage2",))

    assert called == ["Stage 2"]
    assert results[0].status == "success"
    report = extracted_spectrum_report(
        path,
        expected_instrument="NIRSPEC",
        expected_exposure_type="NRS_BRIGHTOBJ",
        expected_detector="NRS2",
    )
    assert all(report["metadata_matches_branch"].values())
    assert report["checks"]["positive_integration_count"] is True
    assert report["checks"]["finite_extracted_flux"] is True
    assert report["checks"]["finite_wavelength_solution"] is True
    assert report["checks"]["monotonic_wavelength_per_integration"] is True
    assert report["wavelength"]["detector_gap_configuration"] is True
    assert report["wavelength"]["coverage_by_order_microns"]["1"] == {
        "minimum": 3.82,
        "maximum": 5.18,
    }
    assert report["spectral_qa"]["total_wavelength_bins"] == 4
    assert report["spectral_qa"]["accepted_for_qa"] == 4
    assert report["spectral_qa"]["rejected"] == 0


def _pathological_nirspec_x1dints(path: Path) -> tuple[np.ndarray, np.ndarray]:
    integrations = 20
    wavelength = np.linspace(2.8, 5.2, 10)
    common_mode = np.ones(integrations)
    common_mode[8:12] = 0.98
    flux = common_mode[:, None] * np.linspace(0.03, 0.015, wavelength.size)[None, :]
    flux[:, -2] = 1e3 * (1 + 0.5 * np.sin(np.arange(integrations)))
    dq = np.zeros_like(flux, dtype=np.uint32)
    dq[:, -1] = 1
    primary = fits.PrimaryHDU()
    for key, value in {
        "INSTRUME": "NIRSPEC",
        "EXP_TYPE": "NRS_BRIGHTOBJ",
        "DETECTOR": "NRS1",
        "GRATING": "G395M",
        "FILTER": "F290LP",
        "SUBARRAY": "SUB2048",
        "NINTS": integrations,
    }.items():
        primary.header[key] = value
    table = fits.BinTableHDU.from_columns(
        [
            fits.Column(name="INT_NUM", format="J", array=np.arange(1, integrations + 1)),
            fits.Column(
                name="TDB-MID",
                format="D",
                array=60_000 + np.arange(integrations) / 1000,
            ),
            fits.Column(
                name="WAVELENGTH",
                format=f"{wavelength.size}D",
                unit="um",
                array=np.tile(wavelength, (integrations, 1)),
            ),
            fits.Column(name="FLUX", format=f"{wavelength.size}D", unit="Jy", array=flux),
            fits.Column(name="DQ", format=f"{wavelength.size}J", array=dq),
        ],
        name="EXTRACT1D",
    )
    table.header["SPORDER"] = -1
    fits.HDUList([primary, table]).writeto(path)
    return common_mode, flux


def test_nirspec_stage3_plots_reject_extreme_channels_and_compare_official_white_light(
    monkeypatch, tmp_path: Path
) -> None:
    x1dints = tmp_path / "g395m_nrs1_x1dints.fits"
    whtlt = tmp_path / "g395m_nrs1_whtlt.ecsv"
    common_mode, flux = _pathological_nirspec_x1dints(x1dints)
    Table(
        {
            "MJD_UTC_NRS1": 60_000 + np.arange(common_mode.size) / 1000,
            "BJD_TDB_NRS1": 60_001 + np.arange(common_mode.size) / 1000,
            "whitelight_flux_NRS1": np.sum(flux, axis=1),
        }
    ).write(whtlt, format="ascii.ecsv")
    uncentered_proxy = 1e3 * (common_mode - 1) + 4.0
    uncentered_proxy[1] = np.nan
    uncentered_proxy[2] = np.inf
    monkeypatch.setattr(nirspec, "qa_common_mode_ppt", lambda selection: uncentered_proxy.copy())
    figures = []

    def capture(figure, output: Path) -> Path:
        figures.append(figure)
        return output

    monkeypatch.setattr(nirspec, "save_figure", capture)
    nirspec.generate(
        x1dints,
        tmp_path,
        stage="Stage 3",
        white_light_path=whtlt,
    )

    spectra_axis = figures[0].axes[0]
    assert spectra_axis.get_ylabel() == F_LAMBDA_LABEL
    assert np.isnan(spectra_axis.lines[0].get_ydata()[-2])
    assert spectra_axis.get_ylim()[1] < 1e-12

    proxy_axis, official_axis = figures[1].axes
    plotted_proxy = proxy_axis.lines[0].get_ydata()
    finite_proxy = np.isfinite(uncentered_proxy)
    expected_proxy = uncentered_proxy - np.median(uncentered_proxy[finite_proxy])
    np.testing.assert_allclose(plotted_proxy, expected_proxy, equal_nan=True)
    assert np.median(plotted_proxy[np.isfinite(plotted_proxy)]) == pytest.approx(0.0)
    assert np.isnan(plotted_proxy[1])
    assert np.isposinf(plotted_proxy[2])
    assert proxy_axis.get_ylabel() == "Robust QA common mode [ppt]"
    assert "Official JWST WhiteLightStep" in official_axis.get_title()
    official_flux = np.sum(flux, axis=1)
    expected_official = 1e3 * (official_flux / np.median(official_flux) - 1.0)
    np.testing.assert_allclose(official_axis.lines[0].get_ydata(), expected_official)
    assert np.ptp(expected_official) > 500

    official = nirspec.read_official_white_light(whtlt, "NRS1")
    assert official is not None
    assert official.time_column == "BJD_TDB_NRS1"
    assert official.flux_column == "whitelight_flux_NRS1"


def test_nirspec_stage2_qa_common_mode_is_median_centered(monkeypatch, tmp_path: Path) -> None:
    x1dints = tmp_path / "g395m_nrs1_x1dints.fits"
    common_mode, _ = _pathological_nirspec_x1dints(x1dints)
    uncentered_proxy = 1e3 * (common_mode - 1) + 4.0
    monkeypatch.setattr(nirspec, "qa_common_mode_ppt", lambda selection: uncentered_proxy.copy())
    figures = []

    def capture(figure, output: Path) -> Path:
        figures.append(figure)
        return output

    monkeypatch.setattr(nirspec, "save_figure", capture)
    nirspec.generate(x1dints, tmp_path, stage="Stage 2")

    plotted_proxy = figures[1].axes[0].lines[0].get_ydata()
    expected = uncentered_proxy - np.median(uncentered_proxy)
    np.testing.assert_allclose(plotted_proxy, expected)
    assert np.median(plotted_proxy) == pytest.approx(0.0)


def test_nirspec_quicklook_plot_and_native_filtered_curve_are_written(tmp_path: Path) -> None:
    x1dints = tmp_path / "g395m_nrs1_x1dints.fits"
    common_mode, _ = _pathological_nirspec_x1dints(x1dints)

    generated = nirspec.generate(
        x1dints,
        tmp_path,
        stage="Stage 2",
        quicklook_cadence_minutes=3.0,
    )

    assert {path.name for path in generated} == {
        "spectra.png",
        "white_light.png",
        "white_light_quicklook.png",
        "white_light_filtered.ecsv",
        "spectroscopic_time_series.png",
        "scatter_spectrum.png",
        "point_to_point_difference.png",
    }
    table = Table.read(tmp_path / "white_light_filtered.ecsv", format="ascii.ecsv")
    assert "order" not in table.colnames
    assert len(table) == common_mode.size
    assert table.colnames == [
        "time",
        "elapsed_time_hours",
        "original_flux_ppt",
        "filtered_flux_ppt",
        "isolated_outlier",
        "rejected",
    ]
    assert table.meta["quicklook_cadence_minutes"] == 3.0
    assert table.meta["source_mode"] == "NIRSpec/BOTS"
    assert not np.any(table["rejected"])
