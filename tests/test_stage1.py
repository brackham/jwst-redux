import json
from dataclasses import replace
from pathlib import Path

import pytest

from jwst_redux.config import (
    DiscoveryConfig,
    ExposureSelectionConfig,
    QueryConfig,
    WriteConfig,
)
from jwst_redux.datasets import build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import expected_stage1_outputs
from jwst_redux.stage1 import run_selected_stage1


class FakeDownloader:
    def __init__(self) -> None:
        self.calls = 0

    def download_product(self, uri: str, destination: Path):
        self.calls += 1
        destination.write_bytes(b"raw!")
        return "COMPLETE", None, None


class FakePipeline:
    def __init__(self, *, fail: bool = False, save_ramp: bool = True) -> None:
        self.fail = fail
        self.save_ramp = save_ramp
        self.calls: list[tuple[Path, Path, dict]] = []

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append((input_path, output_dir, overrides))
        if self.fail:
            raise RuntimeError("synthetic Detector1 failure")
        rate, rateints = expected_stage1_outputs(input_path, output_dir)
        rate.write_bytes(b"rate")
        rateints.write_bytes(b"ints")
        if self.save_ramp:
            stem = input_path.name.removesuffix("_uncal.fits")
            (output_dir / f"{stem}_ramp.fits").write_bytes(b"ramp")


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
    result = DiscoveryResult(datasets=build_science_datasets((exposure,)))
    discovery = DiscoveryConfig(
        query=QueryConfig(
            target="TOI-3884",
            target_match="mast_targname",
            archive_target_names=("TOI-3884",),
            instrument="NIRISS",
            exposure_type="NIS_SOSS",
        ),
        start_from="uncal",
        output_root=tmp_path / "work" / "toi3884",
    )
    config = WriteConfig(
        discovery=discovery,
        selection=ExposureSelectionConfig(
            program_id="05799",
            observation_id="001",
            visit_number="001",
            exposure_id=exposure.exposure_id,
        ),
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        overwrite=False,
    )
    return config, result


def test_stage1_creates_workspace_invokes_pipeline_and_records_success(
    tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    downloader = FakeDownloader()
    pipeline = FakePipeline()

    result = run_selected_stage1(
        config,
        discoverer=lambda _: discovery,
        downloader=downloader,
        pipeline=pipeline,
        context_resolver=lambda _: "jwst_test.pmap",
    )

    root = config.discovery.output_root
    assert all(
        (root / name).is_dir() for name in ("raw", "stage1", "stage2", "stage3", "logs")
    )
    assert (root / "manifest.json").is_file()
    assert downloader.calls == 1
    assert pipeline.calls == [(result.download.path, root.resolve() / "stage1", {})]
    assert [path.name for path in result.outputs] == [
        "jw05799001001_04101_00001-seg001_nis_rate.fits",
        "jw05799001001_04101_00001-seg001_nis_rateints.fits",
        "jw05799001001_04101_00001-seg001_nis_ramp.fits",
    ]
    assert result.log_path.is_file()
    assert "Detector1Pipeline input=" in result.log_path.read_text(encoding="utf-8")

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["runs"][-1]
    assert entry["status"] == "success"
    assert entry["scientific_target"] == "TOI-3884"
    assert entry["scientific_dataset"] == {
        "program_id": "05799",
        "observation_id": "001",
        "visit_number": "001",
    }
    assert entry["exposure_identifier"] == "jw05799001001_04101_00001"
    assert entry["segment_number"] == 1
    assert entry["archive_dataset_id"] == "238871685"
    assert entry["mast_product_uri"].startswith("mast:JWST/product/")
    assert entry["expected_raw_size_bytes"] == 4
    assert entry["download"]["size_bytes"] == 4
    assert entry["pipeline_class"] == "jwst.pipeline.Detector1Pipeline"
    assert entry["explicit_parameter_overrides"] == {}
    assert entry["crds_context"] == "jwst_test.pmap"
    assert len(entry["outputs"]) == 3
    assert entry["start_time"] < entry["end_time"]


def test_stage1_records_failure_and_pipeline_traceback(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    pipeline = FakePipeline(fail=True)

    with pytest.raises(PipelineExecutionError, match="Detector1Pipeline failed"):
        run_selected_stage1(
            config,
            discoverer=lambda _: discovery,
            downloader=FakeDownloader(),
            pipeline=pipeline,
            context_resolver=lambda _: "jwst_test.pmap",
        )

    manifest = json.loads(
        (config.discovery.output_root / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["runs"][-1]
    assert entry["status"] == "failed"
    assert "synthetic Detector1 failure" in entry["error"]
    assert "synthetic Detector1 failure" in Path(entry["log_path"]).read_text(encoding="utf-8")


def test_stage1_resume_and_overwrite_behavior(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    downloader = FakeDownloader()
    pipeline = FakePipeline()
    arguments = {
        "discoverer": lambda _: discovery,
        "downloader": downloader,
        "pipeline": pipeline,
        "context_resolver": lambda _: "jwst_test.pmap",
    }

    first = run_selected_stage1(config, **arguments)
    second = run_selected_stage1(config, **arguments)
    third = run_selected_stage1(config, overwrite=True, **arguments)

    assert first.status == "success"
    assert second.status == "skipped"
    assert second.download.reused is True
    assert second.elapsed_seconds == 0
    assert "skipped" in second.log_path.read_text(encoding="utf-8")
    assert third.status == "success"
    assert third.download.reused is False
    assert downloader.calls == 2
    assert len(pipeline.calls) == 2

    manifest = json.loads(
        (config.discovery.output_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert [entry["status"] for entry in manifest["runs"]] == [
        "success",
        "skipped",
        "success",
    ]
    assert manifest["runs"][1]["resumed_from_run_id"] == manifest["runs"][0]["run_id"]
