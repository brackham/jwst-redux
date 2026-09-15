"""Workflow-level retention behavior and deletion-safety coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.config import DiscoveryConfig, ExposureSelectionConfig, QueryConfig, WriteConfig
from jwst_redux.datasets import build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.provenance import ManifestStore
from jwst_redux.retention import apply_retention
from jwst_redux.stage1 import run_selected_through
from jwst_redux.workspace import Workspace


class FakeDownloader:
    def download_product(self, uri: str, destination: Path):
        destination.write_bytes(b"raw!")
        return "COMPLETE", None, None


class FakeDetector1:
    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        rate, rateints = expected_stage1_outputs(input_path, output_dir)
        rate.write_bytes(b"rate")
        rateints.write_bytes(b"rateints")
        (output_dir / "unrelated.fits").write_bytes(b"leave me")


class FakeSpec2:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        if self.fail:
            raise RuntimeError("synthetic Spec2 failure")
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        calints.write_bytes(b"calints")
        x1dints.write_bytes(b"x1dints")


class FakeTso3:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def __call__(self, association_path: Path, output_dir: Path, overrides: dict) -> None:
        if self.fail:
            raise RuntimeError("synthetic TSO3 failure")
        association = json.loads(association_path.read_text(encoding="utf-8"))
        product_name = association["products"][0]["name"]
        (output_dir / f"{product_name}_x1dints.fits").write_bytes(b"stage3")
        (output_dir / f"{product_name}_whtlt.ecsv").write_bytes(b"white")


def _case(
    tmp_path: Path, exposure_records, *, retention: str
) -> tuple[WriteConfig, DiscoveryResult]:
    exposure_id = "jw05799001001_04101_00001"
    product = Product(
        uri=f"mast:JWST/product/{exposure_id}-seg001_nis_uncal.fits",
        filename=f"{exposure_id}-seg001_nis_uncal.fits",
        size_bytes=4,
        exposure_id=exposure_id,
        suffix="_uncal",
        product_type="science",
        segment_number=1,
        access="PUBLIC",
    )
    exposure = replace(
        normalize_exposure(exposure_records[0]), segment_count=1, products=(product,)
    )
    discovery = DiscoveryResult(build_science_datasets((exposure,)))
    config = WriteConfig(
        discovery=DiscoveryConfig(
            QueryConfig(
                "TOI-3884",
                "mast_targname",
                ("TOI-3884",),
                "NIRISS",
                "NIS_SOSS",
            ),
            "uncal",
            tmp_path / "work",
        ),
        selection=ExposureSelectionConfig("05799", "001", "001", exposure_id),
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        tso3_parameter_overrides={},
        overwrite=False,
        retention=retention,
    )
    return config, discovery


def _run(config, discovery, *, through: str, detector1=None, spec2=None, tso3=None):
    return run_selected_through(
        config,
        through=through,
        discoverer=lambda _: discovery,
        downloader=FakeDownloader(),
        detector1_pipeline=detector1 or FakeDetector1(),
        spec2_pipeline=spec2 or FakeSpec2(),
        tso3_pipeline=tso3 or FakeTso3(),
        context_resolver=lambda _: "jwst_test.pmap",
    )


def _workspace(config: WriteConfig) -> Workspace:
    return Workspace.for_selection(config.discovery.output_root, config.selection)


def test_retention_all_preserves_upstream_products(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="all")

    result = _run(config, discovery, through="stage3")

    assert result.retention is not None and result.retention.status == "skipped"
    assert result.retention.reason == "policy retains all products"
    assert all(path.is_file() for path in result.retention.candidate_paths)
    assert not result.retention.deleted_paths


@pytest.mark.parametrize(
    ("endpoint", "removed_stages", "retained_stage"),
    [
        ("stage1", (), "stage1"),
        ("stage2", ("stage1",), "stage2"),
        ("stage3", ("stage1", "stage2"), "stage3"),
    ],
)
def test_retention_final_keeps_only_endpoint_pipeline_products(
    tmp_path, exposure_records, endpoint, removed_stages, retained_stage
) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")

    result = _run(config, discovery, through=endpoint)
    workspace = _workspace(config)

    assert result.retention is not None
    for stage in removed_stages:
        assert not any(getattr(workspace, stage).glob("*_rate*.fits"))
        assert not any(getattr(workspace, stage).glob("*_calints.fits"))
        assert not any(getattr(workspace, stage).glob("*_x1dints.fits"))
    endpoint_outputs = {
        "stage1": result.segments[0].stage1.outputs,
        "stage2": result.segments[0].stage2.outputs if result.segments[0].stage2 else (),
        "stage3": result.stage3.outputs if result.stage3 else (),
    }[retained_stage]
    assert endpoint_outputs and all(path.is_file() for path in endpoint_outputs)
    assert next(workspace.raw.glob("*_uncal.fits")).is_file()
    if endpoint == "stage1":
        assert result.retention.status == "skipped"
        assert not result.retention.deleted_paths
    else:
        assert result.retention.status == "success"
        assert set(result.retention.deleted_paths) == set(result.retention.candidate_paths)


@pytest.mark.parametrize(
    ("through", "spec2", "tso3", "message"),
    [
        ("stage2", FakeSpec2(fail=True), FakeTso3(), "Spec2Pipeline failed"),
        ("stage3", FakeSpec2(), FakeTso3(fail=True), "Tso3Pipeline failed"),
    ],
)
def test_pipeline_failure_never_triggers_cleanup(
    tmp_path, exposure_records, through, spec2, tso3, message
) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")

    with pytest.raises(PipelineExecutionError, match=message):
        _run(config, discovery, through=through, spec2=spec2, tso3=tso3)

    workspace = _workspace(config)
    assert any(workspace.stage1.glob("*.fits"))
    if through == "stage3":
        assert any(workspace.stage2.glob("*.fits"))
    manifest = json.loads(workspace.manifest.read_text(encoding="utf-8"))
    assert not any(entry["operation"] == "retention" for entry in manifest["runs"])


def test_stage1_failure_never_triggers_cleanup(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")

    def fail_detector1(input_path, output_dir, overrides):
        raise RuntimeError("synthetic Detector1 failure")

    with pytest.raises(PipelineExecutionError, match="Detector1Pipeline failed"):
        _run(config, discovery, through="stage1", detector1=fail_detector1)

    workspace = _workspace(config)
    assert next(workspace.raw.glob("*_uncal.fits")).is_file()
    manifest = json.loads(workspace.manifest.read_text(encoding="utf-8"))
    assert not any(entry["operation"] == "retention" for entry in manifest["runs"])


def test_qa_runs_before_cleanup_and_failure_preserves_products(
    monkeypatch, tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")
    config = replace(config, qa_enabled=True)
    saw_intermediates: list[tuple[str, ...]] = []

    def failing_qa(write_config, stages):
        workspace = _workspace(write_config)
        saw_intermediates.append(stages)
        assert any(workspace.stage1.glob("*_rateints.fits"))
        if stages == ("stage2",):
            assert any(workspace.stage2.glob("*_x1dints.fits"))
        return False

    monkeypatch.setattr("jwst_redux.stage1._generate_qa_without_affecting_pipeline", failing_qa)
    result = _run(config, discovery, through="stage2")

    assert saw_intermediates == [("stage1",), ("stage2",)]
    assert result.retention is not None and result.retention.status == "skipped"
    assert "QA did not complete" in str(result.retention.reason)
    assert all(path.is_file() for path in result.retention.candidate_paths)


def test_cleanup_preserves_non_candidates_and_records_audit_details(
    monkeypatch, tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")
    config = replace(config, qa_enabled=True)

    def successful_qa(write_config, stages):
        workspace = _workspace(write_config)
        qa_file = workspace.qa(stages[0]) / "qa-product.txt"
        qa_file.write_text("qa", encoding="utf-8")
        return True

    monkeypatch.setattr("jwst_redux.stage1._generate_qa_without_affecting_pipeline", successful_qa)
    result = _run(config, discovery, through="stage3")
    workspace = _workspace(config)

    retention = result.retention
    assert retention is not None and retention.status == "success"
    assert retention.bytes_reclaimed == sum(
        item["size_bytes"] for item in retention.manifest_entry["candidate_files"]
    )
    assert retention.manifest_entry["candidate_paths"] == [
        str(path) for path in retention.candidate_paths
    ]
    assert (workspace.stage1 / "unrelated.fits").read_bytes() == b"leave me"
    assert all(
        (workspace.qa(stage) / "qa-product.txt").is_file()
        for stage in ("stage1", "stage2", "stage3")
    )
    assert workspace.manifest.is_file()
    assert any(workspace.logs.iterdir())
    assert result.stage3 is not None and result.stage3.association.path.is_file()
    assert all(path.is_file() for path in result.stage3.outputs)


def test_cli_reports_pruned_outputs_from_manifest_sizes(
    monkeypatch, tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records, retention="final")
    workflow = _run(config, discovery, through="stage2")
    monkeypatch.setattr(cli, "load_write_config", lambda _: config)
    monkeypatch.setattr(cli, "run_selected_through", lambda *_args, **_kwargs: workflow)

    result = CliRunner().invoke(cli.app, ["run", "ignored.yaml", "--through", "stage2"])

    assert result.exit_code == 0
    assert "removed by retention" in result.stdout
    assert "Retention: final" in result.stdout
    assert "Removed 2 regenerable upstream products" in result.stdout


@pytest.mark.parametrize("unsafe_kind", ["symlink", "outside"])
def test_cleanup_refuses_unsafe_recorded_candidates(
    tmp_path, exposure_records, unsafe_kind
) -> None:
    config, _ = _case(tmp_path, exposure_records, retention="final")
    workspace = _workspace(config)
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    manifest.initialize()
    outside = tmp_path / "outside.fits"
    outside.write_bytes(b"outside")
    if unsafe_kind == "symlink":
        candidate = workspace.stage1 / "linked_rateints.fits"
        candidate.symlink_to(outside)
    else:
        candidate = outside
    endpoint = workspace.stage2 / "endpoint_calints.fits"
    endpoint.write_bytes(b"endpoint")
    stage_entries = {
        "stage1": ({"outputs": [{"path": str(candidate), "size_bytes": outside.stat().st_size}]},),
        "stage2": ({"outputs": [{"path": str(endpoint), "size_bytes": endpoint.stat().st_size}]},),
    }

    result = apply_retention(config, "stage2", stage_entries, qa_succeeded=True)

    assert result.status == "skipped"
    assert "candidate safety verification failed" in str(result.reason)
    assert outside.read_bytes() == b"outside"
    assert endpoint.read_bytes() == b"endpoint"
    if unsafe_kind == "symlink":
        assert candidate.is_symlink()


def test_cleanup_requires_intact_endpoint_outputs(tmp_path, exposure_records) -> None:
    config, _ = _case(tmp_path, exposure_records, retention="final")
    workspace = _workspace(config)
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    manifest.initialize()
    candidate = workspace.stage1 / "recorded_rateints.fits"
    candidate.write_bytes(b"candidate")
    missing_endpoint = workspace.stage2 / "missing_calints.fits"
    stage_entries = {
        "stage1": (
            {"outputs": [{"path": str(candidate), "size_bytes": candidate.stat().st_size}]},
        ),
        "stage2": ({"outputs": [{"path": str(missing_endpoint), "size_bytes": 10}]},),
    }

    result = apply_retention(config, "stage2", stage_entries, qa_succeeded=True)

    assert result.status == "skipped"
    assert "endpoint output verification failed" in str(result.reason)
    assert candidate.read_bytes() == b"candidate"
