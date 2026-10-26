"""Public program 1128 nod fixtures and official association regression tests."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from astropy.io import fits
from fixedslit_fixtures import write_pair

from jwst_redux.associations import generate_level3_associations, resolve_level2_associations
from jwst_redux.batch import prepare_batch, run_batch
from jwst_redux.config import BatchConfig, DiscoveryConfig, QueryConfig
from jwst_redux.datasets import build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.query import DiscoveryResult, discover
from jwst_redux.pipeline.runner import expected_stage1_outputs, expected_stage2_outputs
from jwst_redux.pipeline.stages import pipeline_path_for
from jwst_redux.planning.associations import build_compatible_groups
from jwst_redux.planning.resolver import make_reduction_plans
from jwst_redux.qa.workflow import generate_qa
from jwst_redux.stage1 import run_selected_through
from jwst_redux.workspace import Workspace

FIXTURES = Path(__file__).parent / "fixtures" / "fixedslit"


def _records():
    return json.loads((FIXTURES / "exposures.json").read_text())


def _products():
    return json.loads((FIXTURES / "products.json").read_text())


class PublicFixtureClient:
    def query_exposures(self, criteria, select_columns):
        assert "nirspec_fxd_slit" in select_columns
        assert criteria["exp_type"] == "NRS_FIXEDSLIT"
        return _records()

    def list_products(self, exposure_ids):
        return [p for p in _products() if p["dataset"] in exposure_ids]


def _config(tmp_path):
    return BatchConfig(
        DiscoveryConfig(
            QueryConfig(
                "1808347",
                "mast_targname",
                ("TYC 4433-1800-1",),
                "NIRSPEC",
                "NRS_FIXEDSLIT",
                ("1128",),
                "6",
            ),
            "uncal",
            tmp_path / "work",
        ),
        "planned",
        "auto",
        {},
        {},
        {},
        False,
        "continue",
        qa_enabled=True,
        pipeline_overrides={"Spec3Pipeline": {"steps": {"master_background": {"skip": True}}}},
    )


def _discovery(tmp_path, *, grating="G235M"):
    cfg = _config(tmp_path)
    result = discover(cfg.discovery, client=PublicFixtureClient())
    exposures = tuple(
        replace(e, products=tuple(replace(p, size_bytes=2880) for p in e.products))
        for e in result.exposures
        if e.grating == grating
    )
    return cfg, DiscoveryResult(build_science_datasets(exposures))


def _header(filename):
    exposure_id = filename.split("_nrs", 1)[0]
    record = next(r for r in _records() if r["fileSetName"] == exposure_id)
    return fits.Header(
        {
            "PROGRAM": "01128",
            "OBSERVTN": "006",
            "VISIT": "001",
            "TARGNAME": record["targname"],
            "INSTRUME": "NIRSPEC",
            "EXP_TYPE": "NRS_FIXEDSLIT",
            "DETECTOR": "NRS2" if "_nrs2_" in filename else "NRS1",
            "FILTER": record["filter"],
            "GRATING": record["opticalElements"].split(";")[1],
            "FXD_SLIT": "S200A1",
            "SUBARRAY": "SUBS200A1",
            "TSOVISIT": False,
            "PATT_NUM": int(exposure_id.rsplit("_", 1)[-1]),
            "NUMDTHPT": 3,
            "SUBPXPTS": 1,
            "PATTTYPE": "3-POINT-NOD",
            # EXPSPCIN is intentionally absent, matching the real primary header.
        }
    )


class Downloader:
    def __init__(self):
        self.calls = []

    def download_product(self, uri, destination):
        self.calls.append(uri)
        name = destination.name.removesuffix(".part")
        if name.endswith("_asn.json"):
            destination.write_bytes((FIXTURES / name).read_bytes())
        else:
            fits.PrimaryHDU(header=_header(name)).writeto(destination)
        return "COMPLETE", None, None


class Detector1:
    def __init__(self):
        self.calls = []

    def __call__(self, path, output_dir, overrides):
        self.calls.append(path)
        assert os.environ["CRDS_CONTEXT"] == "jwst_test.pmap"
        assert overrides == {}
        # A fixed-slit invocation need only save rate; rateints is not required.
        for output in expected_stage1_outputs(path, output_dir, is_tso=False):
            fits.PrimaryHDU(header=fits.getheader(path)).writeto(output, overwrite=True)


class Spec2:
    def __init__(self, *, fail_nod=None):
        self.calls = []
        self.fail_nod = fail_nod

    def __call__(self, path, output_dir, overrides):
        self.calls.append(path)
        data = json.loads(path.read_text())
        assert data["asn_type"] == "spec2"
        science = next(m for m in data["products"][0]["members"] if m["exptype"] == "science")
        rate = path.parent / science["expname"]
        header = fits.getheader(rate)
        if header["PATT_NUM"] == self.fail_nod:
            raise RuntimeError("synthetic nod failure")
        for member in data["products"][0]["members"]:
            assert (path.parent / member["expname"]).is_file()
            assert member["expname"].endswith("_rate.fits")
        cal, x1d = expected_stage2_outputs(path, output_dir)
        fits.PrimaryHDU(header=header).writeto(cal, overwrite=True)
        x1d.write_bytes(b"extracted spectrum")
        cal.with_name(cal.name.replace("_cal.fits", "_s2d.fits")).write_bytes(b"rectified")


class Spec3:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, path, output_dir, overrides):
        self.calls.append(path)
        assert overrides == {"steps": {"master_background": {"skip": True}}}
        assert os.environ["CRDS_CONTEXT"] == "jwst_test.pmap"
        print("2026-01-01 00:00:00,000 - stpipe.step - INFO - Step Spec3Pipeline parameters are:")
        print("  save_results: True")
        if self.fail:
            raise RuntimeError("synthetic Spec3 failure")
        data = json.loads(path.read_text())
        assert data["asn_type"] == "spec3"
        assert data["asn_rule"] == "Asn_Lv3NRSFSS"
        product = data["products"][0]
        for m in product["members"]:
            assert (path.parent / m["expname"]).is_file()
        name = product["name"].format(source_id="s00001", slit_name="s200a1")
        header = fits.getheader(path.parent / product["members"][0]["expname"])
        write_pair(output_dir / name, header)
        (output_dir / "pipeline_auxiliary.ecsv").write_bytes(b"auxiliary")


def _run(config, discovery, *, detector=None, spec2=None, spec3=None, downloader=None):
    prepared = prepare_batch(config, discoverer=lambda _: discovery)
    return run_batch(
        config,
        prepared=prepared,
        downloader=downloader or Downloader(),
        detector1_pipeline=detector or Detector1(),
        spec2_pipeline=spec2 or Spec2(),
        spec3_pipeline=spec3 or Spec3(),
        context_resolver=lambda _: "jwst_test.pmap",
    )


def test_public_fixedslit_discovery_and_planning(tmp_path):
    config = _config(tmp_path)
    result = discover(config.discovery, client=PublicFixtureClient())
    assert len(result.datasets) == 1
    assert len(result.exposures) == 6
    assert len(result.products) == 12  # Both archive detector arrays are preserved.
    assert all(e.fixed_slit == "S200A1" and e.is_tso is False for e in result.exposures)
    assert all(e.segment_count is None for e in result.exposures)  # MAST EXSEGTOT=0.
    assert all(e.association_products for e in result.exposures)
    plans = make_reduction_plans(config.discovery, result.datasets)
    reductions = plans[0].reductions
    assert len(reductions) == 2  # Grating separation, not BOTS detector branches.
    assert sorted(len(r.group.products) for r in reductions) == [3, 6]
    for reduction in reductions:
        first, second, third = reduction.stages
        assert [s.name for s in reduction.stages] == [
            "Detector1Pipeline",
            "Spec2Pipeline",
            "Spec3Pipeline",
        ]
        assert first.output_suffixes == ("_rate",)
        assert second.input_suffix == "_rate" and second.output_suffixes == ("_cal", "_x1d")
        assert second.association_required
        assert third.input_suffix == "_cal" and third.association_required
        assert all(p.name.endswith("_rate.fits") for p in second.inputs)
        assert not any("rateints" in str(p) for p in second.inputs)
    assert not config.discovery.output_root.exists()


@pytest.mark.parametrize(
    "field,value",
    [("fixed_slit", "S200A2"), ("grating", "G395H"), ("filter", "F290LP"), ("subarray", "FULL")],
)
def test_optical_compatibility_does_not_merge_distinct_configurations(tmp_path, field, value):
    _, discovery = _discovery(tmp_path, grating="G235H")
    a, b = discovery.exposures[:2]
    dataset = replace(discovery.datasets[0], exposures=(a, replace(b, **{field: value})))
    # A different optical configuration can have no useful raw detector; either
    # fail closed or produce distinct groups, never combine incompatible members.
    from jwst_redux.exceptions import PlanningError

    try:
        groups = build_compatible_groups(dataset)
    except PlanningError:
        return
    assert len(groups) == 2


def test_dataset_identity_keeps_observations_and_visits_separate(tmp_path):
    _, result = _discovery(tmp_path)
    e = result.exposures[0]
    datasets = build_science_datasets(
        (e, replace(e, visit_number="002"), replace(e, observation_id="007"))
    )
    assert len(datasets) == 3
    assert pipeline_path_for(e).classes[-1] == "Spec3Pipeline"


def test_official_archive_nod_resolution_is_deterministic_and_preserves_members(tmp_path):
    _, result = _discovery(tmp_path)
    products = build_compatible_groups(result.datasets[0])[0].products
    rates = tuple(tmp_path / p.filename.replace("_uncal", "_rate") for p in products)
    archive = tuple(p for e in result.exposures for p in e.association_products)
    first = resolve_level2_associations(archive, rates, tmp_path / "asn", downloader=Downloader())
    second = resolve_level2_associations(archive, rates, tmp_path / "asn", downloader=Downloader())
    assert len(first) == 3
    assert [a.path for a in first] == [a.path for a in second]
    assert all(a.reused for a in second)
    for a in first:
        data = json.loads(a.path.read_text())
        assert data["asn_rule"].endswith("Asn_Lv2NRSFSS")
        assert [m["exptype"] for m in data["products"][0]["members"]].count("background") == 2
        assert a.source["content_sha256"]


def test_archive_resolution_refuses_missing_nod_and_missing_association(tmp_path):
    _, result = _discovery(tmp_path)
    products = build_compatible_groups(result.datasets[0])[0].products
    rates = tuple(tmp_path / p.filename.replace("_uncal", "_rate") for p in products)
    archive = tuple(p for e in result.exposures for p in e.association_products)
    with pytest.raises(PipelineExecutionError, match="outside the complete selected group"):
        resolve_level2_associations(archive, rates[:2], tmp_path / "asn", downloader=Downloader())
    with pytest.raises(PipelineExecutionError, match="No archived Level-2"):
        resolve_level2_associations((), rates, tmp_path / "asn", downloader=Downloader())


@pytest.mark.parametrize("grating,inputs", [("G235M", 3), ("G235H", 6)])
def test_group_workflow_resume_provenance_and_official_level3(tmp_path, grating, inputs):
    config, discovery = _discovery(tmp_path, grating=grating)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not first.failures
    assert len(first.branches) == 1
    workflow = first.branches[0].workflow
    assert len(workflow.segments) == inputs
    assert len(detector.calls) == len(spec2.calls) == inputs
    assert len(spec3.calls) == 1
    stage3 = workflow.stage3
    assert stage3.status == "success"
    entry = stage3.manifest_entry
    assert entry["pipeline_class"] == "jwst.pipeline.Spec3Pipeline"
    assert entry["crds_context"] == "jwst_test.pmap"
    assert entry["software"]["jwst_redux_git_commit"]
    assert entry["association"]["official_rule"] == "Asn_Lv3NRSFSS"
    assert len(entry["association"]["upstream_stage2_run_ids"]) == inputs
    assert len(stage3.outputs) == 3
    assert entry["pipeline_configuration"]["resolved_parameters"] == {"save_results": True}
    assert "Spec3Pipeline association=" in stage3.log_path.read_text()
    data = json.loads(stage3.association.path.read_text())
    assert len(data["products"][0]["members"]) == inputs
    assert {Path(m["expname"]).suffix for m in data["products"][0]["members"]} == {".fits"}
    if grating == "G235H":
        assert {
            "nrs1" if "_nrs1_" in m["expname"] else "nrs2" for m in data["products"][0]["members"]
        } == {"nrs1", "nrs2"}
    workspace = Workspace.for_config(first.branches[0].branch.config)
    manifest = json.loads(workspace.manifest.read_text())["runs"]
    assert any(e["operation"] == "qa" and e["status"] == "success" for e in manifest)
    assert generate_qa(first.branches[0].branch.config)[0].status == "skipped"
    second = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not second.failures
    assert second.branches[0].workflow.stage3.status == "skipped"
    assert len(detector.calls) == len(spec2.calls) == inputs
    assert len(spec3.calls) == 1
    assert second.branches[0].workflow.stage3.association.path == stage3.association.path


def test_spec3_failure_resume_overwrite_and_deleted_output(tmp_path):
    config, discovery = _discovery(tmp_path)
    failed = _run(config, discovery, spec3=Spec3(fail=True))
    assert len(failed.failures) == 1
    workspace = Workspace.for_config(failed.branches[0].branch.config)
    entry = json.loads(workspace.manifest.read_text())["runs"][-1]
    assert entry["status"] == "failed" and entry["operation"] == "stage3"
    assert "synthetic Spec3 failure" in entry["error"]
    assert entry["association"]["content_sha256"]
    assert "synthetic Spec3 failure" in Path(entry["log_path"]).read_text()
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    success = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not success.failures and not detector.calls and not spec2.calls
    success.branches[0].workflow.stage3.outputs[0].unlink()
    rerun = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not rerun.failures and len(spec3.calls) == 2
    overwritten = _run(
        replace(config, overwrite=True), discovery, detector=detector, spec2=spec2, spec3=spec3
    )
    assert not overwritten.failures
    assert len(detector.calls) == len(spec2.calls) == 3 and len(spec3.calls) == 3


def test_final_retention_waits_for_all_nods_and_preserves_raw_associations_logs(tmp_path):
    config, discovery = _discovery(tmp_path)
    config = replace(config, retention="final")
    result = _run(config, discovery)
    assert not result.failures
    branch = result.branches[0]
    workspace = Workspace.for_config(branch.branch.config)
    assert branch.workflow.retention.status == "success"
    assert not list(workspace.stage1.glob("*.fits"))
    assert not list(workspace.stage2.glob("*.fits"))
    assert len(list(workspace.raw.glob("*.fits"))) == 3
    assert list(workspace.stage3.glob("*.fits"))
    assert branch.workflow.stage3.association.path.is_file()
    assert list((workspace.stage2 / "associations" / "archive").glob("*.json"))
    # Intact final provenance permits resume without regenerating retained-away inputs.
    detector = Detector1()
    again = _run(config, discovery, detector=detector)
    assert not again.failures and len(detector.calls) == 0
    assert again.branches[0].workflow.stage3.status == "skipped"


@pytest.mark.parametrize("retention", ["all", "final"])
def test_completed_group_backfills_qa_without_any_calibration(tmp_path, retention):
    config, discovery = _discovery(tmp_path)
    config = replace(config, retention=retention, qa_enabled=False)
    first = _run(config, discovery)
    branch = first.branches[0]
    workspace = Workspace.for_config(branch.branch.config)
    before = json.loads(workspace.manifest.read_text())["runs"]
    science = {p: p.read_bytes() for p in workspace.root.rglob("*.fits")}
    associations = {p: p.read_bytes() for p in workspace.root.rglob("*_asn.json")}
    assert not list(workspace.qa("stage3").rglob("*.png"))

    def forbidden(*args, **kwargs):
        pytest.fail("completed-group QA backfill executed a calibration")

    again = _run(
        replace(config, qa_enabled=True),
        discovery,
        detector=forbidden,
        spec2=forbidden,
        spec3=forbidden,
    )
    assert not again.failures
    assert again.branches[0].workflow.stage3.status == "skipped"
    assert len(list(workspace.qa("stage3").rglob("*.png"))) == 3
    after = json.loads(workspace.manifest.read_text())["runs"]
    assert after[: len(before)] == before
    assert any(e["operation"] == "qa" and e["status"] == "success" for e in after[len(before) :])
    assert {p: p.read_bytes() for p in science} == science
    assert {p: p.read_bytes() for p in associations} == associations
    if retention == "final":
        assert not list(workspace.stage1.glob("*.fits"))
        assert not list(workspace.stage2.glob("*.fits"))
    third = _run(
        replace(config, qa_enabled=True),
        discovery,
        detector=forbidden,
        spec2=forbidden,
        spec3=forbidden,
    )
    assert not third.failures
    entries = json.loads(workspace.manifest.read_text())["runs"]
    assert next(e for e in reversed(entries) if e["operation"] == "group")["qa_status"] == "success"
    assert next(e for e in reversed(entries) if e["operation"] == "qa")["status"] == "skipped"


def test_automatic_qa_failure_visible_and_blocks_final_retention(monkeypatch, tmp_path):
    from jwst_redux.qa import fixedslit

    def fail(*args, **kwargs):
        raise ValueError("synthetic QA failure")

    monkeypatch.setattr(fixedslit, "generate", fail)
    config, discovery = _discovery(tmp_path)
    with pytest.warns(UserWarning, match="Stage-3 QA failed"):
        result = _run(replace(config, retention="final"), discovery)
    assert not result.failures  # official calibration remains successful
    workspace = Workspace.for_config(result.branches[0].branch.config)
    entries = json.loads(workspace.manifest.read_text())["runs"]
    assert any(e["operation"] == "qa" and e["status"] == "failed" for e in entries)
    assert next(e for e in reversed(entries) if e["operation"] == "group")["qa_status"] == "failed"
    assert result.branches[0].workflow.retention.status == "skipped"
    assert list(workspace.stage2.glob("*.fits"))


def test_stage2_failure_keeps_background_inputs_and_successful_siblings(tmp_path):
    config, discovery = _discovery(tmp_path)
    config = replace(config, retention="final")
    spec2 = Spec2(fail_nod=2)
    failed = _run(config, discovery, spec2=spec2)
    assert failed.failures
    workspace = Workspace.for_config(failed.branches[0].branch.config)
    assert len(list(workspace.stage1.glob("*_rate.fits"))) == 3
    assert len(list(workspace.stage2.glob("*_cal.fits"))) == 1
    fixed = Spec2()
    resumed = _run(config, discovery, spec2=fixed)
    assert not resumed.failures and len(fixed.calls) == 2


def test_selected_run_expands_to_the_complete_nod_group(tmp_path):
    config, discovery = _discovery(tmp_path)
    branch = prepare_batch(config, discoverer=lambda _: discovery).branches[0]
    result = run_selected_through(
        branch.config,
        through="stage2",
        discoverer=lambda _: discovery,
        downloader=Downloader(),
        detector1_pipeline=Detector1(),
        spec2_pipeline=Spec2(),
        context_resolver=lambda _: "jwst_test.pmap",
    )
    assert len(result.segments) == 3 and result.stage3 is None
    assert len(result.stage3_readiness.calints_inputs) == 3


def test_level3_generation_rejects_bad_headers_and_is_schema_valid(tmp_path):
    p = tmp_path / "jw01128006001_0310c_00001_nrs1_cal.fits"
    header = _header(p.name)
    fits.PrimaryHDU(header=header).writeto(p)
    first = generate_level3_associations((p,), tmp_path / "asn", "Asn_Lv3NRSFSS")
    second = generate_level3_associations((p,), tmp_path / "asn", "Asn_Lv3NRSFSS")
    assert second[0].path == first[0].path and second[0].reused
    del header["FXD_SLIT"]
    fits.PrimaryHDU(header=header).writeto(p, overwrite=True)
    with pytest.raises(PipelineExecutionError, match="FXD_SLIT"):
        generate_level3_associations((p,), tmp_path / "asn", "Asn_Lv3NRSFSS")


def test_query_grating_filter_narrow_real_public_fixture(tmp_path):
    config = _config(tmp_path)
    discovery_config = replace(
        config.discovery, query=replace(config.discovery.query, grating="G235M", filter="F170LP")
    )
    result = discover(discovery_config, client=PublicFixtureClient())
    assert len(result.exposures) == 3
    assert all(e.grating == "G235M" for e in result.exposures)


def test_preflight_missing_associations_never_downloads_science_or_resolves_crds(tmp_path):
    config, discovery = _discovery(tmp_path)
    discovery = DiscoveryResult(
        build_science_datasets(
            tuple(replace(e, association_products=()) for e in discovery.exposures)
        )
    )
    downloader = Downloader()
    prepared = prepare_batch(config, discoverer=lambda _: discovery)

    def forbidden_context(_):
        pytest.fail("CRDS accessed before association preflight")

    result = run_batch(
        config, prepared=prepared, downloader=downloader, context_resolver=forbidden_context
    )
    assert result.failures and downloader.calls == []
    workspace = Workspace.for_config(result.branches[0].branch.config)
    entry = json.loads(workspace.manifest.read_text())["runs"][-1]
    assert entry["operation"] == "group" and entry["status"] == "failed"
    assert "No archived Level-2" in entry["error"]


def test_official_detector_rule_requires_both_high_resolution_arrays(tmp_path):
    _, discovery = _discovery(tmp_path, grating="G235H")
    exposure = discovery.exposures[0]
    incomplete = replace(
        exposure, products=tuple(p for p in exposure.products if p.detector == "NRS1")
    )
    from jwst_redux.exceptions import PlanningError

    with pytest.raises(PlanningError, match="required detector products"):
        build_compatible_groups(replace(discovery.datasets[0], exposures=(incomplete,)))


def test_ambiguous_archive_membership_fails_closed(tmp_path):
    _, discovery = _discovery(tmp_path)
    group = build_compatible_groups(discovery.datasets[0])[0]
    rates = tuple(tmp_path / p.filename.replace("_uncal", "_rate") for p in group.products)
    archive = tuple(p for e in group.exposures for p in e.association_products)
    source = archive[0]
    data = json.loads((FIXTURES / source.filename).read_text())
    data["products"][0]["members"].pop()  # Contradictory but schema-valid archived revision.
    extra = replace(
        source, filename="zz_alternate_asn.json", uri="archive:alternate", size_bytes=None
    )

    class AlternateDownloader(Downloader):
        def download_product(self, uri, destination):
            if uri == extra.uri:
                destination.write_text(json.dumps(data))
                return "COMPLETE", None, None
            return super().download_product(uri, destination)

    with pytest.raises(PipelineExecutionError, match="Ambiguous"):
        resolve_level2_associations(
            (*archive, extra), rates, tmp_path / "asn", downloader=AlternateDownloader()
        )


def test_non_tso_detector1_records_optional_rateints_without_requiring_it(tmp_path):
    from jwst_redux.pipeline.runner import run_detector1

    path = tmp_path / "example_uncal.fits"
    path.write_bytes(b"raw")

    def pipeline(path, directory, overrides):
        (directory / "example_rate.fits").write_bytes(b"rate")
        (directory / "example_rateints.fits").write_bytes(b"optional")

    result = run_detector1(
        path,
        tmp_path,
        tmp_path / "detector.log",
        "jwst_test.pmap",
        {},
        pipeline=pipeline,
        is_tso=False,
    )
    assert {p.name for p in result.outputs} == {"example_rate.fits", "example_rateints.fits"}
    from jwst_redux.stage1 import _file_record

    assert _file_record(result.outputs[0], "_rate")["role"] == "stage2_input"
    assert _file_record(result.outputs[1], "_rate")["role"] == "stage1_integration_intermediate"


def test_official_spec3_class_invocation_and_model_cleanup(monkeypatch, tmp_path):
    from jwst.pipeline import Spec3Pipeline

    from jwst_redux.pipeline.runner import run_spec3

    path = tmp_path / "asn.json"
    path.write_text("{}")
    calls = []
    closed = []

    class Model:
        def close(self):
            closed.append(True)

    def call(association, **parameters):
        calls.append((association, parameters))
        assert os.environ["CRDS_CONTEXT"] == "jwst_test.pmap"
        (Path(parameters["output_dir"]) / "source_x1d.fits").write_bytes(b"official output")
        return [Model(), Model()]

    monkeypatch.setattr(Spec3Pipeline, "call", call)
    previous = os.environ.get("CRDS_CONTEXT")
    result = run_spec3(
        path,
        tmp_path,
        tmp_path / "spec3.log",
        "jwst_test.pmap",
        {"steps": {"outlier_detection": {"skip": True}}},
    )
    assert closed == [True, True]
    assert calls == [
        (
            str(path),
            {
                "output_dir": str(tmp_path),
                "save_results": True,
                "steps": {"outlier_detection": {"skip": True}},
            },
        )
    ]
    assert os.environ.get("CRDS_CONTEXT") == previous
    assert tmp_path / "source_x1d.fits" in result.outputs


def test_official_spec2_resolves_relative_members_from_association_directory(monkeypatch, tmp_path):
    from jwst.pipeline import Spec2Pipeline

    from jwst_redux.pipeline.runner import run_spec2

    _, discovery = _discovery(tmp_path)
    group = build_compatible_groups(discovery.datasets[0])[0]
    stage1 = tmp_path / "stage1"
    stage1.mkdir()
    rates = tuple(stage1 / p.filename.replace("_uncal", "_rate") for p in group.products)
    for path in rates:
        path.write_bytes(b"rate")
    archive = tuple(p for e in group.exposures for p in e.association_products)
    output_dir = tmp_path / "stage2"
    output_dir.mkdir()
    association = resolve_level2_associations(
        archive, rates, output_dir / "associations", downloader=Downloader()
    )[0]
    previous = Path.cwd()

    def call(path, **parameters):
        assert Path.cwd() == association.path.parent
        data = json.loads(Path(path).read_text())
        # Model STScI's Spec2 loader: paths containing directories are not
        # prefixed with input_dir, so nested relative paths resolve from cwd.
        for m in data["products"][0]["members"]:
            assert Path(m["expname"]).is_file()
        for output in expected_stage2_outputs(Path(path), Path(parameters["output_dir"])):
            output.write_bytes(b"output")

    monkeypatch.setattr(Spec2Pipeline, "call", call)
    result = run_spec2(association.path, output_dir, tmp_path / "spec2.log", "jwst_test.pmap", {})
    assert len(result.outputs) == 2
    assert Path.cwd() == previous


def test_bookkeeping_commit_does_not_reprocess_intact_pipeline_products(monkeypatch, tmp_path):
    from jwst_redux.provenance import software_versions

    versions = software_versions()
    current = {**versions, "jwst_redux_git_commit": "before-commit"}
    monkeypatch.setattr("jwst_redux.pipeline.workflow.software_versions", lambda: dict(current))
    config, discovery = _discovery(tmp_path)
    spec2, spec3 = Spec2(), Spec3()
    first = _run(config, discovery, spec2=spec2, spec3=spec3)
    assert not first.failures
    current.update(jwst_redux_git_commit="after-commit", jwst_redux_git_dirty=False)
    second = _run(config, discovery, spec2=spec2, spec3=spec3)
    assert not second.failures
    assert second.branches[0].workflow.stage3.status == "skipped"
    assert len(spec2.calls) == 3 and len(spec3.calls) == 1
    # An official pipeline version change invalidates that match.
    current["jwst_version"] = "next-jwst-version"
    third = _run(config, discovery, spec2=spec2, spec3=spec3)
    assert not third.failures
    assert third.branches[0].workflow.stage3.status == "success"
    assert len(spec2.calls) == 6 and len(spec3.calls) == 2


def test_existing_group_manifests_with_representative_selectors_still_resume(tmp_path):
    config, discovery = _discovery(tmp_path)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not first.failures
    workspace = Workspace.for_config(first.branches[0].branch.config)
    data = json.loads(workspace.manifest.read_text())
    representative = next(e["selection"] for e in data["runs"] if e["operation"] == "stage1")
    for entry in data["runs"]:
        if entry["operation"] in {"stage1", "stage2"} and entry["status"] == "success":
            entry["selection"] = representative
            entry["run_key"] = "legacy-representative-key"
    workspace.manifest.write_text(json.dumps(data))
    resumed = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not resumed.failures
    assert len(detector.calls) == len(spec2.calls) == 3
    assert len(spec3.calls) == 1


def test_legacy_pruned_group_reuses_endpoint_only_with_matching_dependency_chain(tmp_path):
    config, discovery = _discovery(tmp_path)
    config = replace(config, retention="final")
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not first.failures
    workspace = Workspace.for_config(first.branches[0].branch.config)
    data = json.loads(workspace.manifest.read_text())
    representative = next(e["selection"] for e in data["runs"] if e["operation"] == "stage1")
    for entry in data["runs"]:
        if entry["operation"] == "group":
            for key in (
                "checkpoint_key",
                "level2_association_hashes",
                "endpoint_run_id",
                "outputs",
                "associations",
                "retention_policy",
                "retention_status",
            ):
                entry.pop(key, None)
        elif entry["operation"] in {"stage1", "stage2"}:
            entry["selection"] = representative
            entry["run_key"] = "legacy-key"
    workspace.manifest.write_text(json.dumps(data))
    again = _run(config, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not again.failures
    assert again.branches[0].workflow.stage3.status == "skipped"
    assert len(detector.calls) == len(spec2.calls) == 3 and len(spec3.calls) == 1
    changed = replace(config, spec2_parameter_overrides={"steps": {"bkg_subtract": {"skip": True}}})
    invalidated = _run(changed, discovery, detector=detector, spec2=spec2, spec3=spec3)
    assert not invalidated.failures
    assert invalidated.branches[0].workflow.stage3.status == "success"
    assert len(detector.calls) == len(spec2.calls) == 6 and len(spec3.calls) == 2
