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
        rateints.write_bytes(b"rateints")


class FakeSpec2:
    def __init__(self, *, fail_segment: int | None = None) -> None:
        self.calls: list[Path] = []
        self.fail_segment = fail_segment

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append(input_path)
        segment = int(input_path.name.split("-seg", maxsplit=1)[1][:3])
        if segment == self.fail_segment:
            raise RuntimeError(f"synthetic Spec2 failure for segment {segment}")
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        calints.write_bytes(b"calints")
        x1dints.write_bytes(b"x1dints")


class FakeTso3:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[Path, Path, dict]] = []
        self.fail = fail

    def __call__(self, association_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append((association_path, output_dir, overrides))
        print("2026-01-01 00:00:00,000 - stpipe.step - INFO - Step Tso3Pipeline parameters are:")
        print("  save_results: True")
        if self.fail:
            raise RuntimeError("synthetic TSO3 failure")
        association = json.loads(association_path.read_text(encoding="utf-8"))
        product_name = association["products"][0]["name"]
        (output_dir / f"{product_name}_x1dints.fits").write_bytes(b"x1dints")
        (output_dir / f"{product_name}_whtlt.ecsv").write_bytes(b"whtlt")
        for member in association["products"][0]["members"]:
            member_name = Path(member["expname"]).name.removesuffix("_calints.fits")
            (output_dir / f"{member_name}_a3001_crfints.fits").write_bytes(b"crfints")


def _case(tmp_path: Path, exposure_records) -> tuple[WriteConfig, DiscoveryResult]:
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
        for segment in (1, 2, 3)
    )
    selected_exposure = replace(
        normalize_exposure(exposure_records[0]), segment_count=3, products=products
    )
    unrelated = replace(normalize_exposure(exposure_records[1]), products=())
    discovery = DiscoveryResult(
        datasets=build_science_datasets((selected_exposure, unrelated))
    )
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


def _arguments(discovery, downloader, detector1, spec2, tso3):
    return {
        "through": "stage3",
        "discoverer": lambda _: discovery,
        "downloader": downloader,
        "detector1_pipeline": detector1,
        "spec2_pipeline": spec2,
        "tso3_pipeline": tso3,
        "context_resolver": lambda _: "jwst_test.pmap",
    }


def test_stage3_resolves_manifest_inputs_constructs_association_and_records_outputs(
    tmp_path, exposure_records
) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    detector1 = FakeDetector1()
    spec2 = FakeSpec2()
    tso3 = FakeTso3()

    result = run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), detector1, spec2, tso3),
    )

    assert result.stage3 is not None and result.stage3.status == "success"
    assert len(tso3.calls) == 1
    assert [path.name for path in result.stage3.readiness.calints_inputs] == [
        "jw05799001001_04101_00001-seg001_nis_calints.fits",
        "jw05799001001_04101_00001-seg002_nis_calints.fits",
        "jw05799001001_04101_00001-seg003_nis_calints.fits",
    ]
    assert not any("02001" in path.name for path in detector1.calls + spec2.calls)

    association_path = result.stage3.association.path
    association = json.loads(association_path.read_text(encoding="utf-8"))
    assert association["asn_rule"] == "DMS_Level3_Base"
    assert association["products"][0]["name"] == "jw05799001001_04101_00001_tso3"
    assert [Path(member["expname"]).name for member in association["products"][0]["members"]] == [
        "jw05799001001_04101_00001-seg001_nis_calints.fits",
        "jw05799001001_04101_00001-seg002_nis_calints.fits",
        "jw05799001001_04101_00001-seg003_nis_calints.fits",
    ]
    assert association_path.parent == config.discovery.output_root / "stage3" / "associations"
    assert {
        (association_path.parent / member["expname"]).resolve()
        for member in association["products"][0]["members"]
    } == set(result.stage3.readiness.calints_inputs)

    entry = result.stage3.manifest_entry
    assert entry["operation"] == "stage3"
    assert entry["pipeline_class"] == "jwst.pipeline.Tso3Pipeline"
    assert entry["association"]["content_sha256"] == result.stage3.association.content_sha256
    assert len(entry["association"]["upstream_stage2_run_ids"]) == 3
    assert {output["role"] for output in entry["outputs"]} == {
        "tso3_combined_extracted_spectrum",
        "tso3_white_light_curve",
        "tso3_outlier_flagged_integrations",
    }
    assert result.stage3.log_path.is_file()
    assert "Tso3Pipeline association=" in result.stage3.log_path.read_text(encoding="utf-8")


def test_stage3_refuses_missing_manifest_calints_before_pipeline(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), FakeTso3()),
    )
    stage3_readiness(config, discoverer=lambda _: discovery).calints_inputs[1].unlink()

    with pytest.raises(PipelineExecutionError, match="segment 002"):
        stage3_readiness(config, discoverer=lambda _: discovery)


def test_stage3_readiness_refuses_unsuccessful_expected_stage2_run(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    with pytest.raises(PipelineExecutionError, match="synthetic Spec2 failure for segment 2"):
        run_selected_through(
            config,
            **_arguments(
                discovery,
                FakeDownloader(),
                FakeDetector1(),
                FakeSpec2(fail_segment=2),
                FakeTso3(),
            ),
        )

    with pytest.raises(PipelineExecutionError, match="segment 002"):
        stage3_readiness(config, discoverer=lambda _: discovery)


def test_stage3_failure_resume_membership_change_and_overwrite(tmp_path, exposure_records) -> None:
    config, discovery = _case(tmp_path, exposure_records)
    first = FakeTso3(fail=True)
    with pytest.raises(PipelineExecutionError, match="synthetic TSO3 failure"):
        run_selected_through(
            config,
            **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), first),
        )
    manifest_path = config.discovery.output_root / "manifest.json"
    failed = json.loads(manifest_path.read_text(encoding="utf-8"))["runs"][-1]
    assert failed["operation"] == "stage3"
    assert failed["status"] == "failed"
    assert len(failed["association"]["member_calints_paths"]) == 3
    assert len(failed["association"]["content_sha256"]) == 64
    assert "synthetic TSO3 failure" in failed["error"]
    assert "synthetic TSO3 failure" in Path(failed["log_path"]).read_text(encoding="utf-8")

    successful = FakeTso3()
    second = run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), successful),
    )
    assert second.stage3 is not None and second.stage3.status == "success"
    association_path = second.stage3.association.path

    resumed = run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), successful),
    )
    assert resumed.stage3 is not None and resumed.stage3.status == "skipped"
    assert resumed.stage3.association.path == association_path
    assert len(successful.calls) == 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original = next(
        entry
        for entry in reversed(manifest["runs"])
        if entry["operation"] == "stage2" and entry["status"] == "success"
    )
    alternate = config.discovery.output_root / "stage2" / "alternate-seg003_nis_calints.fits"
    alternate.write_bytes(b"alternate-calints")
    changed = {
        **original,
        "run_id": "changed-stage2-member",
        "outputs": [
            {**record, "path": str(alternate), "size_bytes": alternate.stat().st_size}
            if record["product_type"] == "calints"
            else record
            for record in original["outputs"]
        ],
    }
    manifest["runs"].append(changed)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    changed_membership = run_selected_through(
        config,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), successful),
    )
    assert changed_membership.stage3 is not None
    assert changed_membership.stage3.status == "success"
    assert changed_membership.stage3.association.path != association_path
    assert len(successful.calls) == 2

    overwritten = run_selected_through(
        config,
        overwrite=True,
        **_arguments(discovery, FakeDownloader(), FakeDetector1(), FakeSpec2(), successful),
    )
    assert overwritten.stage3 is not None and overwritten.stage3.status == "success"
    assert len(successful.calls) == 3
