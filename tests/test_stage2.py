import json
from dataclasses import replace
from pathlib import Path

import pytest

from jwst_redux.config import (
    DiscoveryConfig,
    QueryConfig,
    Stage1SelectionConfig,
    WriteConfig,
)
from jwst_redux.datasets import build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.stage1 import resolve_stage1_rateints, run_selected_through


class FakeDownloader:
    def __init__(self) -> None:
        self.calls = 0

    def download_product(self, uri: str, destination: Path):
        self.calls += 1
        destination.write_bytes(b"raw!")
        return "COMPLETE", None, None


class FakeDetector1:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append(input_path)
        rate, rateints = expected_stage1_outputs(input_path, output_dir)
        rate.write_bytes(b"rate")
        rateints.write_bytes(b"ints")


class FakeSpec2:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Path, Path, dict]] = []

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append((input_path, output_dir, overrides))
        print("2026-01-01 00:00:00,000 - stpipe.step - INFO - Step Spec2Pipeline parameters are:")
        print("  save_results: True")
        print("  steps:")
        print("    extract_1d:")
        print("      skip: False")
        print("2026-01-01 00:00:00,001 - test - WARNING - synthetic Spec2 warning")
        if self.fail:
            raise RuntimeError("synthetic Spec2 failure")
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        calints.write_bytes(b"calints")
        x1dints.write_bytes(b"x1dints")


def _case(tmp_path: Path, exposure_records) -> tuple[WriteConfig, DiscoveryResult]:
    product = Product(
        uri="mast:JWST/product/jw05799001001_04101_00001-seg001_nis_uncal.fits",
        filename="jw05799001001_04101_00001-seg001_nis_uncal.fits",
        size_bytes=4,
        exposure_id="jw05799001001_04101_00001",
        suffix="_uncal",
        product_type="science",
        segment_number=1,
        access="PUBLIC",
    )
    exposure = replace(
        normalize_exposure(exposure_records[0]),
        segment_count=1,
        products=(product,),
    )
    discovery = DiscoveryResult(datasets=build_science_datasets((exposure,)))
    config = WriteConfig(
        discovery=DiscoveryConfig(
            query=QueryConfig(
                target="TOI-3884",
                target_match="mast_targname",
                archive_target_names=("TOI-3884",),
                instrument="NIRISS",
                exposure_type="NIS_SOSS",
            ),
            start_from="uncal",
            output_root=tmp_path / "work" / "toi3884",
        ),
        selection=Stage1SelectionConfig(
            program_id="05799",
            observation_id="001",
            visit_number="001",
            exposure_id=exposure.exposure_id,
            segment_number=1,
            filename=product.filename,
        ),
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        overwrite=False,
    )
    return config, discovery


def test_stage2_input_is_resolved_from_successful_stage1_manifest(tmp_path: Path) -> None:
    recorded = tmp_path / "manifest-selected_rateints.fits"
    recorded.write_bytes(b"recorded")
    guessed = tmp_path / "guessed_rateints.fits"
    guessed.write_bytes(b"guessed")
    entry = {
        "operation": "stage1",
        "status": "success",
        "outputs": [
            {
                "path": str(recorded),
                "size_bytes": recorded.stat().st_size,
                "product_type": "rateints",
            }
        ],
    }

    assert resolve_stage1_rateints(entry) == recorded

    entry["outputs"] = []
    with pytest.raises(PipelineExecutionError, match="exactly one"):
        resolve_stage1_rateints(entry)


def test_stage2_invocation_outputs_and_provenance(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    detector1 = FakeDetector1()
    spec2 = FakeSpec2()

    result = run_selected_through(
        config,
        through="stage2",
        discoverer=lambda _: discovery,
        downloader=FakeDownloader(),
        detector1_pipeline=detector1,
        spec2_pipeline=spec2,
        context_resolver=lambda _: "jwst_test.pmap",
    )

    assert result.stage2 is not None
    stage2 = result.stage2
    assert spec2.calls == [(stage2.input_path, config.discovery.output_root.resolve() / "stage2", {})]
    manifest = json.loads(
        (config.discovery.output_root / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["runs"][-1]
    assert entry["status"] == "success"
    assert entry["pipeline_class"] == "jwst.pipeline.Spec2Pipeline"
    assert entry["input_path"] == str(stage2.input_path)
    assert entry["input_product_type"] == "rateints"
    assert entry["upstream_stage1_run_id"] == result.stage1.manifest_entry["run_id"]
    assert [output["product_type"] for output in entry["outputs"]] == [
        "calints",
        "x1dints",
    ]
    assert entry["outputs"][0]["future_tso3_association_input"] is True
    assert entry["outputs"][1]["future_tso3_association_input"] is False
    assert entry["pipeline_configuration"]["resolved_parameters"] == {
        "save_results": True,
        "steps": {"extract_1d": {"skip": False}},
    }
    assert entry["pipeline_configuration"]["invocation"]["explicit_parameter_overrides"] == {}
    assert entry["pipeline_messages"]["warning_count"] == 1
    assert entry["pipeline_messages"]["warnings"] == ["synthetic Spec2 warning"]


def test_stage2_failure_is_recorded(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)

    with pytest.raises(PipelineExecutionError, match="Spec2Pipeline failed"):
        run_selected_through(
            config,
            through="stage2",
            discoverer=lambda _: discovery,
            downloader=FakeDownloader(),
            detector1_pipeline=FakeDetector1(),
            spec2_pipeline=FakeSpec2(fail=True),
            context_resolver=lambda _: "jwst_test.pmap",
        )

    manifest = json.loads(
        (config.discovery.output_root / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["runs"][-1]
    assert entry["operation"] == "stage2"
    assert entry["status"] == "failed"
    assert "synthetic Spec2 failure" in entry["error"]
    assert entry["pipeline_messages"]["warning_count"] == 1
    assert "synthetic Spec2 failure" in Path(entry["log_path"]).read_text(encoding="utf-8")


def test_stage2_resume_and_overwrite(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    downloader = FakeDownloader()
    detector1 = FakeDetector1()
    spec2 = FakeSpec2()
    arguments = {
        "through": "stage2",
        "discoverer": lambda _: discovery,
        "downloader": downloader,
        "detector1_pipeline": detector1,
        "spec2_pipeline": spec2,
        "context_resolver": lambda _: "jwst_test.pmap",
    }

    first = run_selected_through(config, **arguments)
    manifest_path = config.discovery.output_root / "manifest.json"
    legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in legacy_manifest["runs"]:
        if entry["status"] == "success":
            entry["run_key"] = f"legacy-{entry['operation']}"
    manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    second = run_selected_through(config, **arguments)
    third = run_selected_through(config, overwrite=True, **arguments)

    assert first.stage2 is not None and first.stage2.status == "success"
    assert second.stage1.status == "skipped"
    assert second.stage2 is not None and second.stage2.status == "skipped"
    assert second.stage2.elapsed_seconds == 0
    assert third.stage2 is not None and third.stage2.status == "success"
    assert downloader.calls == 2
    assert len(detector1.calls) == 2
    assert len(spec2.calls) == 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stage2_entries = [entry for entry in manifest["runs"] if entry["operation"] == "stage2"]
    assert [entry["status"] for entry in stage2_entries] == ["success", "skipped", "success"]
    assert stage2_entries[1]["resumed_from_run_id"] == stage2_entries[0]["run_id"]
