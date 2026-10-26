"""Program discovery and evolving fixed-slit groups using official ASN fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from astropy.io import fits
from test_fixedslit import (
    FIXTURES,
    Detector1,
    Spec2,
    Spec3,
    _header,
    _products,
    _records,
)
from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.batch import prepare_batch, run_batch
from jwst_redux.config import load_batch_config
from jwst_redux.mast.query import discover
from jwst_redux.provenance import ManifestStore
from jwst_redux.workspace import Workspace


class EvolvingProgram:
    """A synthetic two-target archive; science and schema-valid ASNs evolve together."""

    def __init__(self):
        self.nods = {1: 2}
        self.calls = []
        self.criteria = []

    @staticmethod
    def transform(text, observation):
        return (
            text.replace("jw01128006", f"jw11441{observation:03d}")
            .replace("jw01128-o006", f"jw11441-o{observation:03d}")
            .replace('"01128"', '"11441"')
            .replace('"o006"', f'"o{observation:03d}"')
        )

    def query_exposures(self, criteria, select_columns):
        self.criteria.append(criteria)
        assert criteria["program"] == "11441"
        assert "targname" not in criteria
        records = []
        for observation, count in self.nods.items():
            for original in _records():
                if "G235M" not in original["opticalElements"]:
                    continue
                if int(original["fileSetName"].rsplit("_", 1)[1]) > count:
                    continue
                row = json.loads(self.transform(json.dumps(original), observation))
                row.update(
                    program=11441,
                    observtn=observation,
                    visit_id=f"11441{observation:03d}001",
                    targname=f"Star-{observation}",
                    ArchiveFileID=f"archive-{observation}-{original['ArchiveFileID']}",
                    access="PUBLIC" if observation == 1 else "EXCLUSIVE_ACCESS",
                )
                records.append(row)
        return records

    def list_products(self, exposure_ids):
        records = []
        for observation in self.nods:
            for original in _products():
                row = json.loads(self.transform(json.dumps(original), observation))
                if row["dataset"] in exposure_ids:
                    row["size"] = 2880 if row["file_suffix"] == "_uncal" else None
                    records.append(row)
        return records

    def download_product(self, uri, destination):
        self.calls.append(uri)
        name = destination.name.removesuffix(".part")
        observation = int(uri.split("/", 1)[0][7:10])
        if name.endswith("_asn.json"):
            original = name.replace(f"jw11441-o{observation:03d}", "jw01128-o006")
            data = json.loads(self.transform((FIXTURES / original).read_text(), observation))
            data["products"][0]["members"] = [
                m
                for m in data["products"][0]["members"]
                if int(m["expname"].split("_nrs")[0].rsplit("_", 1)[1]) <= self.nods[observation]
            ]
            destination.write_text(json.dumps(data))
        else:
            original = name.replace(f"jw11441{observation:03d}", "jw01128006")
            header = _header(original)
            header.update(
                PROGRAM="11441", OBSERVTN=f"{observation:03d}", TARGNAME=f"Star-{observation}"
            )
            fits.PrimaryHDU(header=header).writeto(destination)
        return "COMPLETE", None, None


def program_config(tmp_path, *, retention="final"):
    config = load_batch_config(Path(__file__).parents[1] / "configs" / "go11441.yaml")
    return replace(
        config,
        discovery=replace(config.discovery, output_root=tmp_path),
        retention=retention,
        pipeline_overrides={"Spec3Pipeline": {"steps": {"master_background": {"skip": True}}}},
    )


def execute(config, archive, detector, spec2, spec3, *, context="jwst_test.pmap"):
    return run_batch(
        config,
        discoverer=lambda cfg: discover(cfg, archive),
        downloader=archive,
        detector1_pipeline=detector,
        spec2_pipeline=spec2,
        spec3_pipeline=spec3,
        context_resolver=lambda _: context,
    )


def test_proposal_only_discovery_preserves_multiple_targets_and_cli(tmp_path, monkeypatch):
    archive = EvolvingProgram()
    archive.nods[2] = 3
    config = program_config(tmp_path)
    result = discover(config.discovery, archive)
    assert len(result.datasets) == 2
    assert {e.target_name for e in result.exposures} == {"Star-1", "Star-2"}
    assert {e.access for e in result.exposures} == {"PUBLIC", "EXCLUSIVE_ACCESS"}
    prepared = prepare_batch(config, discoverer=lambda _: result)
    assert len(prepared.branches) == 2
    assert sorted(len(b.reduction.group.products) for b in prepared.branches) == [2, 3]
    assert len({b.config.group_id for b in prepared.branches}) == 2
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(cli, "discover", lambda _: result)
    source = Path(__file__).parents[1] / "configs" / "go11441.yaml"
    for command in ("search", "plan"):
        output = CliRunner().invoke(cli.app, [command, str(source)])
        assert output.exit_code == 0, output.output
        assert "Proposal 11441" in output.output
        assert "None" not in output.output
        assert "Star-1" in output.output and "Star-2" in output.output


@pytest.mark.parametrize("retention", ["all", "final"])
def test_program_rerun_new_target_and_expanded_nod_group(tmp_path, retention, monkeypatch):
    archive = EvolvingProgram()
    config = program_config(tmp_path, retention=retention)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = execute(config, archive, detector, spec2, spec3)
    assert not first.failures
    assert (len(detector.calls), len(spec2.calls), len(spec3.calls)) == (2, 2, 1)
    old_final = {p: p.read_bytes() for p in first.branches[0].workflow.stage3.outputs}
    old_association = first.branches[0].workflow.stage3.association.path
    raw_downloads = sum(uri.endswith("_uncal.fits") for uri in archive.calls)
    again = execute(config, archive, detector, spec2, spec3)
    assert not again.failures
    assert again.branches[0].workflow.stage3.status == "skipped"
    if retention == "final":
        branch = again.branches[0]
        assert not branch.workflow.segments
        monkeypatch.setattr(cli, "load_write_config", lambda _: branch.branch.config)
        monkeypatch.setattr(cli, "run_selected_through", lambda *_args, **_kwargs: branch.workflow)
        output = CliRunner().invoke(cli.app, ["run", "ignored.yaml", "--through", "stage3"])
        assert output.exit_code == 0, output.output
        assert "Spec3Pipeline skipped" in output.output
        assert "Manifest:" in output.output
    assert (len(detector.calls), len(spec2.calls), len(spec3.calls)) == (2, 2, 1)
    assert sum(uri.endswith("_uncal.fits") for uri in archive.calls) == raw_downloads
    prepared = prepare_batch(config, discoverer=lambda cfg: discover(cfg, archive))
    assert all(action == "resume" for b in prepared.branches for _, action in b.stage_actions)

    archive.nods[2] = 3
    new_target = execute(config, archive, detector, spec2, spec3)
    assert not new_target.failures
    assert new_target.branches[0].workflow.stage3.status == "skipped"
    assert new_target.branches[1].workflow.stage3.status == "success"
    assert (len(detector.calls), len(spec2.calls), len(spec3.calls)) == (5, 5, 2)
    for branch in new_target.branches:
        entry = branch.workflow.stage3.manifest_entry
        assert entry["scientific_target"] == branch.branch.exposure.target_name

    archive.nods[1] = 3  # Same compatibility group, new nod and updated background assignments.
    prepared = prepare_batch(config, discoverer=lambda cfg: discover(cfg, archive))
    assert all(action == "run" for _, action in prepared.branches[0].stage_actions)
    assert all(action == "resume" for _, action in prepared.branches[1].stage_actions)
    expanded = execute(config, archive, detector, spec2, spec3)
    assert not expanded.failures
    branch = expanded.branches[0]
    assert branch.workflow.stage3.status == "success"
    assert expanded.branches[1].workflow.stage3.status == "skipped"
    assert len(detector.calls) == (6 if retention == "all" else 8)
    assert len(spec2.calls) == 8 and len(spec3.calls) == 3
    assert branch.workflow.stage3.association.path != old_association
    data = json.loads(branch.workflow.stage3.association.path.read_text())
    assert len(data["products"][0]["members"]) == 3
    for segment in branch.workflow.segments:
        assert len(segment.stage2.manifest_entry["association"]["members"]) == 3
    assert all(p.read_bytes() == content for p, content in old_final.items())
    assert set(old_final).isdisjoint(branch.workflow.stage3.outputs)
    if retention == "final":
        assert branch.workflow.retention.status == "success"
        assert all(not p.exists() for p in branch.workflow.retention.deleted_paths)
    final = execute(config, archive, detector, spec2, spec3)
    assert not final.failures
    assert all(b.workflow.stage3.status == "skipped" for b in final.branches)
    assert len(spec3.calls) == 3


def test_same_name_archive_revision_invalidates_retained_endpoint(tmp_path):
    archive = EvolvingProgram()
    config = program_config(tmp_path)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = execute(config, archive, detector, spec2, spec3)
    assert not first.failures
    original_download = archive.download_product

    def revised(uri, destination):
        result = original_download(uri, destination)
        if "_asn.json" in uri:
            data = json.loads(destination.read_text())
            data["version_id"] = "new-archive-revision"
            destination.write_text(json.dumps(data))
        return result

    archive.download_product = revised
    revised_result = execute(config, archive, detector, spec2, spec3)
    assert not revised_result.failures
    assert revised_result.branches[0].workflow.stage3.status == "success"
    assert len(spec3.calls) == 2


def test_retained_endpoint_rechecks_overrides_context_and_missing_final(tmp_path):
    archive = EvolvingProgram()
    config = program_config(tmp_path)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    first = execute(config, archive, detector, spec2, spec3)
    assert not first.failures
    first.branches[0].workflow.stage3.outputs[0].unlink()
    damaged = execute(config, archive, detector, spec2, spec3)
    assert not damaged.failures and len(spec3.calls) == 2
    changed = replace(config, parameter_overrides={"steps": {"jump": {"skip": True}}})

    # The new setting is passed through and changes the completion checkpoint.
    def updated_detector(path, output_dir, overrides):
        assert overrides == changed.parameter_overrides
        detector(path, output_dir, {})

    result = execute(changed, archive, updated_detector, spec2, spec3)
    assert not result.failures and len(spec3.calls) == 3
    workspace = Workspace.for_config(result.branches[0].branch.config)
    store = ManifestStore(workspace.manifest, config.discovery.query.label)
    assert store.successful_entry("group", {"crds_context": "different.pmap"}, {}) is None
    # A CRDS change cannot match the old completion checkpoint.
    from jwst_redux.group_resume import completed_group

    branch = result.branches[0].branch
    assert (
        completed_group(store, branch.config, branch.reduction.group, crds_context="different.pmap")
        is None
    )


def test_incomplete_program_resumes_successful_nods_and_continues_new_targets(tmp_path):
    archive = EvolvingProgram()
    config = program_config(tmp_path)
    detector, spec2, spec3 = Detector1(), Spec2(fail_nod=2), Spec3()
    failed = execute(config, archive, detector, spec2, spec3)
    assert len(failed.failures) == 1 and not spec3.calls
    archive.nods[2] = 3
    fixed = Spec2()
    resumed = execute(config, archive, detector, fixed, spec3)
    assert not resumed.failures
    assert len(detector.calls) == 5  # Existing rates reused, only new target runs Detector1.
    assert len(fixed.calls) == 4  # One failed nod plus the new target's three nods.
    assert len(spec3.calls) == 2
    assert resumed.branches[0].workflow.segments[0].stage2.status == "skipped"
    assert all(b.workflow.retention.status == "success" for b in resumed.branches)


def test_retained_endpoint_rechecks_pipeline_version(tmp_path, monkeypatch):
    from jwst_redux.provenance import software_versions

    versions = software_versions()
    monkeypatch.setattr("jwst_redux.pipeline.workflow.software_versions", lambda: dict(versions))
    monkeypatch.setattr("jwst_redux.stage1.software_versions", lambda: dict(versions))
    archive = EvolvingProgram()
    config = program_config(tmp_path)
    detector, spec2, spec3 = Detector1(), Spec2(), Spec3()
    assert not execute(config, archive, detector, spec2, spec3).failures
    versions["jwst_version"] = "next-pipeline-version"
    changed = execute(config, archive, detector, spec2, spec3)
    assert not changed.failures
    assert len(detector.calls) == len(spec2.calls) == 4
    assert len(spec3.calls) == 2
    assert (
        changed.branches[0].workflow.stage3.manifest_entry["software"]["jwst_version"]
        == "next-pipeline-version"
    )
