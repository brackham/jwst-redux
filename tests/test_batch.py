"""Mocked planner-driven batch reduction coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from jwst_redux.batch import prepare_batch, run_batch
from jwst_redux.config import BatchConfig, DiscoveryConfig, QueryConfig
from jwst_redux.datasets import build_science_datasets
from jwst_redux.mast.query import DiscoveryResult, _mast_criteria, normalize_exposure
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.workspace import Workspace


class FakeDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def download_product(self, uri: str, destination: Path):
        self.calls.append(uri)
        destination.write_bytes(b"raw")
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
    def __init__(self, *, fail_exposure: str | None = None) -> None:
        self.calls: list[Path] = []
        self.fail_exposure = fail_exposure

    def __call__(self, input_path: Path, output_dir: Path, overrides: dict) -> None:
        self.calls.append(input_path)
        if self.fail_exposure and self.fail_exposure in input_path.name:
            raise RuntimeError(f"synthetic Spec2 failure for {self.fail_exposure}")
        calints, x1dints = expected_stage2_outputs(input_path, output_dir)
        calints.write_bytes(b"calints")
        x1dints.write_bytes(b"x1dints")


class FakeTso3:
    def __init__(self) -> None:
        self.members: list[list[str]] = []

    def __call__(self, association_path: Path, output_dir: Path, overrides: dict) -> None:
        association = json.loads(association_path.read_text(encoding="utf-8"))
        product = association["products"][0]
        members = [Path(item["expname"]).name for item in product["members"]]
        self.members.append(members)
        (output_dir / f"{product['name']}_x1dints.fits").write_bytes(b"x1dints")
        (output_dir / f"{product['name']}_whtlt.ecsv").write_bytes(b"whtlt")


def _exposure(
    program: int,
    observation: int,
    exposure_number: str,
    *,
    integrations: int,
    optical_elements: str = "CLEAR;GR700XD",
    segments: int = 1,
) -> object:
    visit = "001"
    prefix = f"jw{program:05d}{observation:03d}{visit}"
    exposure_id = f"{prefix}_{exposure_number}_00001"
    record = {
        "ArchiveFileID": int(f"{program}{observation}{exposure_number}"),
        "fileSetName": exposure_id,
        "productLevel": "1b",
        "program": program,
        "observtn": observation,
        "visit": 1,
        "visit_id": prefix,
        "targname": "TOI-3884b" if program == 5863 else "TOI-3884",
        "instrume": "NIRISS",
        "exp_type": "NIS_SOSS",
        "tsovisit": "t",
        "opticalElements": optical_elements,
        "subarray": "SUBSTRIP256",
        "nints": integrations,
        "ngroups": 10,
        "exsegtot": segments,
        "access": "PUBLIC",
        "proposal_type": "GO",
    }
    products = tuple(
        Product(
            uri=f"mast:JWST/product/{exposure_id}-seg{segment:03d}_nis_uncal.fits",
            filename=f"{exposure_id}-seg{segment:03d}_nis_uncal.fits",
            size_bytes=3,
            exposure_id=exposure_id,
            suffix="_uncal",
            product_type="science",
            segment_number=segment,
            access="PUBLIC",
        )
        for segment in range(1, segments + 1)
    )
    return replace(normalize_exposure(record), products=products)


def _discovery() -> DiscoveryResult:
    exposures = (
        _exposure(5799, 1, "04101", integrations=321, segments=3),
        _exposure(5799, 2, "04101", integrations=321, segments=3),
        _exposure(5863, 1, "04101", integrations=1),
        _exposure(5863, 1, "04102", integrations=321),
        _exposure(5863, 1, "04103", integrations=1, optical_elements="F277W;GR700XD"),
        _exposure(5863, 3, "04101", integrations=1),
        _exposure(5863, 3, "04102", integrations=321),
        _exposure(5863, 3, "04103", integrations=1, optical_elements="F277W;GR700XD"),
    )
    return DiscoveryResult(datasets=build_science_datasets(exposures))


def _config(
    root: Path, *, failure_policy: str = "continue", qa_enabled: bool = False
) -> BatchConfig:
    return BatchConfig(
        discovery=DiscoveryConfig(
            QueryConfig(
                "TOI-3884",
                "mast_targname",
                ("TOI-3884", "TOI-3884b"),
                "NIRISS",
                "NIS_SOSS",
                proposal_ids=("5799", "5863"),
            ),
            "uncal",
            root,
        ),
        endpoint="planned",
        crds_context="auto",
        parameter_overrides={},
        spec2_parameter_overrides={},
        tso3_parameter_overrides={},
        overwrite=False,
        failure_policy=failure_policy,
        qa_enabled=qa_enabled,
    )


def _run(config: BatchConfig, prepared, downloader, detector1, spec2, tso3):
    return run_batch(
        config,
        prepared=prepared,
        downloader=downloader,
        detector1_pipeline=detector1,
        spec2_pipeline=spec2,
        tso3_pipeline=tso3,
        context_resolver=lambda _: "jwst_test.pmap",
    )


def test_batch_discovery_proposal_scope_and_heterogeneous_endpoints(tmp_path: Path) -> None:
    config = _config(tmp_path / "work")
    prepared = prepare_batch(config, discoverer=lambda _: _discovery())

    assert len(prepared.discovery.datasets) == 4
    assert len(prepared.discovery.exposures) == 8
    assert config.discovery.query.proposal_ids == ("5799", "5863")
    assert _mast_criteria(config.discovery)["program"] == "5799,5863"
    assert {branch.config.selection.program_id for branch in prepared.branches} == {
        "05799",
        "05863",
    }
    assert len(prepared.branches) == 8
    assert {
        branch.config.selection.resolved_exposure_number: branch.endpoint
        for branch in prepared.branches
        if branch.config.selection.program_id == "05863"
    } == {"04101": "stage2", "04102": "stage3", "04103": "stage1"}


def test_batch_runs_in_planned_order_and_isolates_workspaces_and_associations(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "work")
    prepared = prepare_batch(config, discoverer=lambda _: _discovery())
    detector1 = FakeDetector1()
    spec2 = FakeSpec2()
    tso3 = FakeTso3()

    result = _run(config, prepared, FakeDownloader(), detector1, spec2, tso3)

    assert [item.status for item in result.branches] == ["success"] * 8
    assert all(
        item.workflow is not None
        and item.workflow.stage3 is None
        and item.workflow.stage3_readiness is None
        for item in result.branches
        if item.branch.endpoint == "stage2"
    )
    assert [path.name.split("-seg", maxsplit=1)[0] for path in detector1.calls] == [
        branch.config.selection.exposure_id
        for branch in prepared.branches
        for _ in range(branch.segment_count)
    ]
    assert len(spec2.calls) == 10  # all but the two F277W Detector1-only branches
    assert len(tso3.members) == 4
    assert all(len(members) == 3 for members in tso3.members[:2])
    assert all(len(members) == 1 for members in tso3.members[2:])
    assert all(
        len({name.split("-seg", maxsplit=1)[0] for name in members}) == 1
        for members in tso3.members
    )

    roots = [
        Workspace.for_selection(config.discovery.output_root, branch.config.selection).root
        for branch in prepared.branches
    ]
    assert len(set(roots)) == 8
    assert all(path.is_dir() for path in roots)
    for branch in prepared.branches:
        workspace = Workspace.for_selection(config.discovery.output_root, branch.config.selection)
        manifest = json.loads(workspace.manifest.read_text(encoding="utf-8"))
        assert all(
            entry["selection"]["workspace_name"] == workspace.root.name
            for entry in manifest["runs"]
        )


def test_batch_reuses_completed_work_and_resumes_only_failed_branch(tmp_path: Path) -> None:
    config = _config(tmp_path / "work")
    prepared = prepare_batch(config, discoverer=lambda _: _discovery())
    failure_id = "jw05863001001_04102_00001"
    failed = _run(
        config,
        prepared,
        FakeDownloader(),
        FakeDetector1(),
        FakeSpec2(fail_exposure=failure_id),
        FakeTso3(),
    )
    assert [item.branch.config.selection.exposure_id for item in failed.failures] == [failure_id]
    assert len(failed.branches) == 8  # continue policy preserves later siblings

    detector1 = FakeDetector1()
    spec2 = FakeSpec2()
    resumed = _run(config, prepared, FakeDownloader(), detector1, spec2, FakeTso3())
    assert not resumed.failures
    assert not detector1.calls
    assert [path.name.split("-seg", maxsplit=1)[0] for path in spec2.calls] == [failure_id]
    assert dict(prepared.branches[0].stage_actions)["Detector1Pipeline"] == "run"
    refreshed = prepare_batch(config, discoverer=lambda _: _discovery())
    assert all(
        action == "resume" for branch in refreshed.branches for _, action in branch.stage_actions
    )


def test_batch_qa_is_requested_only_for_stages_at_each_planned_endpoint(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(tmp_path / "work", qa_enabled=True)
    prepared = prepare_batch(config, discoverer=lambda _: _discovery())
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        "jwst_redux.stage1._generate_qa_without_affecting_pipeline",
        lambda write_config, stages: calls.append((write_config.selection.exposure_id, stages)),
    )

    result = _run(config, prepared, FakeDownloader(), FakeDetector1(), FakeSpec2(), FakeTso3())

    assert not result.failures
    f277w_ids = {
        branch.config.selection.exposure_id
        for branch in prepared.branches
        if branch.config.selection.resolved_exposure_number == "04103"
    }
    single_integration_ids = {
        branch.config.selection.exposure_id
        for branch in prepared.branches
        if branch.config.selection.program_id == "05863"
        and branch.config.selection.resolved_exposure_number == "04101"
    }
    assert all(stages == ("stage1",) for exposure_id, stages in calls if exposure_id in f277w_ids)
    assert {stages for exposure_id, stages in calls if exposure_id in single_integration_ids} == {
        ("stage1",),
        ("stage2",),
    }
    assert not any(
        stages == ("stage3",)
        for exposure_id, stages in calls
        if exposure_id in f277w_ids | single_integration_ids
    )


def test_batch_applies_final_retention_independently_per_branch(tmp_path: Path) -> None:
    config = replace(_config(tmp_path / "work"), retention="final")
    prepared = prepare_batch(config, discoverer=lambda _: _discovery())
    failure_id = "jw05863001001_04102_00001"

    result = _run(
        config,
        prepared,
        FakeDownloader(),
        FakeDetector1(),
        FakeSpec2(fail_exposure=failure_id),
        FakeTso3(),
    )

    assert all(branch.config.retention == "final" for branch in prepared.branches)
    assert [item.branch.config.selection.exposure_id for item in result.failures] == [failure_id]
    for item in result.branches:
        workspace = Workspace.for_selection(
            config.discovery.output_root, item.branch.config.selection
        )
        if item.status == "failed":
            assert any(workspace.stage1.glob("*.fits"))
            continue
        assert item.workflow is not None and item.workflow.retention is not None
        if item.branch.endpoint == "stage1":
            assert item.workflow.retention.status == "skipped"
            assert any(workspace.stage1.glob("*.fits"))
        else:
            assert item.workflow.retention.status == "success"
            assert not any(workspace.stage1.glob("*.fits"))
        if item.branch.endpoint == "stage3":
            assert not any(workspace.stage2.glob("*.fits"))
            assert any(workspace.stage3.glob("*.fits"))
