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
from jwst_redux.exceptions import PipelineExecutionError, PlanningError
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.stage1 import run_selected_through, stage3_readiness


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download_product(self, uri: str, destination: Path):
        self.calls.append(uri)
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
    def __init__(self, *, fail_segment: int | None = None) -> None:
        self.fail_segment = fail_segment
        self.calls: list[Path] = []

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append(input_path)
        segment = int(input_path.name.split("-seg", maxsplit=1)[1][:3])
        print("2026-01-01 00:00:00,000 - stpipe.step - INFO - Step Spec2Pipeline parameters are:")
        print("  save_results: True")
        if segment == self.fail_segment:
            raise RuntimeError(f"synthetic Spec2 failure for segment {segment}")
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        calints.write_bytes(b"calints")
        x1dints.write_bytes(b"x1dints")


def _case(
    tmp_path: Path, exposure_records, segments=(1, 2, 3), *, segment_count: int | None = None
) -> tuple[WriteConfig, DiscoveryResult]:
    exposure_id = "jw05799001001_04101_00001"
    products = tuple(
        Product(
            uri=f"mast:JWST/product/{exposure_id}-seg{segment:03d}_nis_uncal.fits",
            filename=f"{exposure_id}-seg{segment:03d}_nis_uncal.fits",
            size_bytes=4,
            exposure_id=exposure_id,
            suffix="_uncal",
            product_type="science",
            segment_number=segment,
            access="PUBLIC",
        )
        for segment in segments
    )
    exposure = replace(
        normalize_exposure(exposure_records[0]),
        segment_count=len(segments) if segment_count is None else segment_count,
        products=products,
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
        selection=ExposureSelectionConfig(
            program_id="05799",
            observation_id="001",
            visit_number="001",
            exposure_id=exposure_id,
        ),
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        tso3_parameter_overrides={},
        overwrite=False,
    )
    return config, discovery


def _arguments(discovery, downloader, detector1, spec2):
    return {
        "through": "stage2",
        "discoverer": lambda _: discovery,
        "downloader": downloader,
        "detector1_pipeline": detector1,
        "spec2_pipeline": spec2,
        "context_resolver": lambda _: "jwst_test.pmap",
    }


def test_selected_exposure_reuses_completed_segment_then_runs_remaining_in_order(
    tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    completed_config, completed_discovery = _case(tmp_path, exposure_records, segments=(1,))
    downloader = FakeDownloader()
    detector1 = FakeDetector1()
    spec2 = FakeSpec2()

    run_selected_through(
        completed_config,
        **_arguments(completed_discovery, downloader, detector1, spec2),
    )
    detector1.calls.clear()
    spec2.calls.clear()
    result = run_selected_through(config, **_arguments(discovery, downloader, detector1, spec2))

    assert [segment.stage1.selected.product.segment_number for segment in result.segments] == [1, 2, 3]
    assert result.segments[0].stage1.status == "skipped"
    assert result.segments[0].stage2 is not None and result.segments[0].stage2.status == "skipped"
    assert [path.name for path in detector1.calls] == [
        "jw05799001001_04101_00001-seg002_nis_uncal.fits",
        "jw05799001001_04101_00001-seg003_nis_uncal.fits",
    ]
    assert [path.name for path in spec2.calls] == [
        "jw05799001001_04101_00001-seg002_nis_rateints.fits",
        "jw05799001001_04101_00001-seg003_nis_rateints.fits",
    ]
    assert result.stage3_readiness is not None
    assert [path.name for path in result.stage3_readiness.calints_inputs] == [
        "jw05799001001_04101_00001-seg001_nis_calints.fits",
        "jw05799001001_04101_00001-seg002_nis_calints.fits",
        "jw05799001001_04101_00001-seg003_nis_calints.fits",
    ]


def test_partial_failure_stops_sequence_and_later_run_resumes_each_segment(
    tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    downloader = FakeDownloader()
    first_detector1 = FakeDetector1()
    with pytest.raises(PipelineExecutionError, match="segment 2"):
        run_selected_through(
            config,
            **_arguments(discovery, downloader, first_detector1, FakeSpec2(fail_segment=2)),
        )

    manifest_path = config.discovery.output_root / "manifest.json"
    failed_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [(entry["operation"], entry["segment_number"], entry["status"]) for entry in failed_manifest["runs"]] == [
        ("stage1", 1, "success"),
        ("stage2", 1, "success"),
        ("stage1", 2, "success"),
        ("stage2", 2, "failed"),
    ]

    second_detector1 = FakeDetector1()
    second_spec2 = FakeSpec2()
    result = run_selected_through(
        config,
        **_arguments(discovery, downloader, second_detector1, second_spec2),
    )
    assert [segment.stage1.status for segment in result.segments] == ["skipped", "skipped", "success"]
    assert [segment.stage2.status for segment in result.segments if segment.stage2] == [
        "skipped",
        "success",
        "success",
    ]
    assert [path.name for path in second_detector1.calls] == [
        "jw05799001001_04101_00001-seg003_nis_uncal.fits"
    ]
    assert [path.name for path in second_spec2.calls] == [
        "jw05799001001_04101_00001-seg002_nis_rateints.fits",
        "jw05799001001_04101_00001-seg003_nis_rateints.fits",
    ]


def test_selected_exposure_requires_complete_segment_set_before_writes(tmp_path, exposure_records) -> None:
    config, incomplete = _case(tmp_path, exposure_records, segments=(1, 2), segment_count=3)

    with pytest.raises(PlanningError, match=r"segments \[1, 2\].*expected \[1, 2, 3\]"):
        run_selected_through(config, through="stage2", discoverer=lambda _: incomplete)

    assert not config.discovery.output_root.exists()


def test_stage3_readiness_requires_every_intact_calints_input(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2()),
    )
    readiness = stage3_readiness(config, discoverer=lambda _: discovery)
    readiness.calints_inputs[1].unlink()

    with pytest.raises(PipelineExecutionError, match="segment 002"):
        stage3_readiness(config, discoverer=lambda _: discovery)
