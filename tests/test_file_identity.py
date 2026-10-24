"""Content-backed resume, legacy proof, and safe retention under timestamp drift."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from astropy.io import fits
from test_fixedslit import Detector1, Spec2, Spec3
from test_proposal import EvolvingProgram, execute, program_config
from typer.testing import CliRunner

from jwst_redux import cli, file_identity
from jwst_redux.exceptions import JWSTReduxError
from jwst_redux.file_identity import file_matches, record_file
from jwst_redux.provenance import ManifestStore
from jwst_redux.retention import apply_retention
from jwst_redux.workspace import Workspace


def drift(paths):
    for path in paths:
        details = path.stat()
        os.utime(path, ns=(details.st_atime_ns, details.st_mtime_ns + 114))


def forbidden(*_args):
    raise RuntimeError("Completed calibration was invoked")


def case(tmp_path, *, retention="all", spec3=None):
    archive = EvolvingProgram()
    config = program_config(tmp_path, retention=retention)
    detector, spec2, spec3 = Detector1(), Spec2(), spec3 or Spec3()
    result = execute(config, archive, detector, spec2, spec3)
    return config, archive, detector, spec2, spec3, result


def test_stable_metadata_avoids_hashing_and_changed_metadata_hashes_once(tmp_path, monkeypatch):
    path = tmp_path / "large.fits"
    path.write_bytes(b"science")
    reference = record_file(path)
    original = file_identity._digest
    calls = []

    def digest(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(file_identity, "_digest", digest)
    assert file_matches(reference)
    assert record_file(path, [reference]) == reference
    assert not calls
    drift([path])
    assert file_matches(reference)
    assert len(calls) == 1
    current = record_file(path)
    calls.clear()
    assert file_matches(reference, [current])
    assert not calls


@pytest.mark.parametrize("restore_mtime", [False, True])
def test_same_size_content_change_is_rejected_even_with_restored_mtime(tmp_path, restore_mtime):
    path = tmp_path / "science.fits"
    path.write_bytes(b"original")
    reference = record_file(path)
    path.write_bytes(b"modified")
    if restore_mtime:
        os.utime(path, ns=(path.stat().st_atime_ns, reference["mtime_ns"]))
    assert not file_matches(reference)


def test_missing_checksum_never_proves_a_changed_timestamp(tmp_path):
    path = tmp_path / "science.fits"
    path.write_bytes(b"science")
    old = record_file(path)
    old.pop("sha256")
    assert file_matches(old)
    drift([path])
    assert not file_matches(old)
    # A newly computed digest with the new timestamp is not historical proof.
    assert not file_matches(old, [record_file(path)])


def test_timestamp_drift_reuses_all_stages_and_preserves_history(tmp_path):
    config, archive, _, _, _, first = case(tmp_path)
    branch = first.branches[0]
    workspace = Workspace.for_config(branch.branch.config)
    original = json.loads(workspace.manifest.read_text())["runs"]
    paths = list(workspace.root.glob("*/*.fits"))
    contents = {p: p.read_bytes() for p in paths}
    drift(paths)
    repeat = execute(config, archive, forbidden, forbidden, forbidden)
    assert not repeat.failures
    flow = repeat.branches[0].workflow
    assert flow.stage3.status == "skipped"
    assert all(s.stage1.status == s.stage2.status == "skipped" for s in flow.segments)
    assert all(p.read_bytes() == contents[p] for p in paths)
    by_id = {e["run_id"]: e for e in json.loads(workspace.manifest.read_text())["runs"]}
    assert all(by_id[e["run_id"]] == e for e in original)
    assert all(e["sha256"] for e in flow.stage3.manifest_entry["outputs"])
    assert not execute(config, archive, forbidden, forbidden, forbidden).failures


def test_partial_group_reuses_stage2_after_drift(tmp_path):
    def failed(*_args):
        raise RuntimeError("interrupted before Spec3")

    config, archive, _, _, _, first = case(tmp_path, spec3=failed)
    assert first.failures
    workspace = Workspace.for_config(first.branches[0].branch.config)
    original_ids = {
        e["run_id"]
        for e in json.loads(workspace.manifest.read_text())["runs"]
        if e.get("operation") == "stage2" and e["status"] == "success"
    }
    drift(list(workspace.root.glob("*/*.fits")))
    resumed = execute(config, archive, forbidden, forbidden, Spec3())
    assert not resumed.failures
    flow = resumed.branches[0].workflow
    assert flow.stage3.status == "success"
    assert set(flow.stage3.manifest_entry["association"]["upstream_stage2_run_ids"]) == original_ids


@pytest.mark.parametrize(
    "stage,suffix,expected",
    [
        ("raw", "_uncal.fits", (3, 4, 2)),
        ("stage1", "_rate.fits", (3, 4, 2)),
        ("stage2", "_cal.fits", (2, 3, 2)),
        ("stage3", "_x1d.fits", (2, 2, 2)),
    ],
)
def test_genuine_changes_invalidate_only_required_work(tmp_path, stage, suffix, expected):
    config, archive, detector, spec2, spec3, first = case(tmp_path)
    workspace = Workspace.for_config(first.branches[0].branch.config)
    path = next(p for p in getattr(workspace, stage).glob("*.fits") if p.name.endswith(suffix))
    size = path.stat().st_size
    if stage == "stage3":  # The fixture's extracted spectrum is a small binary placeholder.
        path.write_bytes(b"X" * size)
    else:
        with fits.open(path, mode="update") as hdus:
            hdus[0].header["TESTSIG"] = "changed bytes"
    assert path.stat().st_size == size
    repeated = execute(config, archive, detector, spec2, spec3)
    assert not repeated.failures
    assert (len(detector.calls), len(spec2.calls), len(spec3.calls)) == expected
    assert repeated.branches[0].workflow.stage3.status == "success"


@pytest.mark.parametrize("changed_stage", ["raw", "stage3"])
def test_pruned_endpoint_survives_drift_but_not_corruption(tmp_path, changed_stage):
    config, archive, detector, spec2, spec3, first = case(tmp_path, retention="final")
    workspace = Workspace.for_config(first.branches[0].branch.config)
    assert not list(workspace.stage2.glob("*.fits"))
    drift(list(workspace.raw.glob("*.fits")) + list(workspace.stage3.glob("*.fits")))
    repeated = execute(config, archive, forbidden, forbidden, forbidden)
    assert not repeated.failures
    assert repeated.branches[0].workflow.stage3.status == "skipped"
    assert not repeated.branches[0].workflow.segments
    changed = next(getattr(workspace, changed_stage).glob("*.fits"))
    if changed_stage == "raw":
        with fits.open(changed, mode="update") as hdus:
            hdus[0].header["TESTSIG"] = "changed bytes"
    else:
        changed.write_bytes(b"X" * changed.stat().st_size)
    regenerated = execute(config, archive, detector, spec2, spec3)
    assert not regenerated.failures
    assert (len(detector.calls), len(spec2.calls), len(spec3.calls)) == (4, 4, 2)


@pytest.mark.parametrize("stage", ["stage1", "stage3", "timestamps"])
def test_retention_verifies_contents_before_any_deletion(tmp_path, stage):
    _, _, _, _, _, first = case(tmp_path)
    branch = first.branches[0]
    flow = branch.workflow
    entries = {
        "stage1": tuple(s.stage1.manifest_entry for s in flow.segments),
        "stage2": tuple(s.stage2.manifest_entry for s in flow.segments),
        "stage3": (flow.stage3.manifest_entry,),
    }
    if stage == "timestamps":
        drift([Path(p["path"]) for es in entries.values() for e in es for p in e["outputs"]])
    else:
        p = Path(entries[stage][0]["outputs"][0]["path"])
        p.write_bytes(b"X" * p.stat().st_size)
    result = apply_retention(
        replace(branch.branch.config, retention="final"), "stage3", entries, qa_succeeded=True
    )
    if stage == "timestamps":
        assert result.status == "success" and result.deleted_paths
    else:
        assert result.status == "skipped" and not result.deleted_paths
        assert all(Path(p["path"]).is_file() for e in entries["stage1"] for p in e["outputs"])


def test_legacy_drift_requires_independent_baseline_and_import_is_audited(tmp_path):
    config, archive, _, _, _, first = case(tmp_path)
    workspace = Workspace.for_config(first.branches[0].branch.config)
    baseline = tmp_path / "trusted-before-drift.json"
    baseline.write_text(
        json.dumps({"files": [record_file(p) for p in workspace.root.glob("*/*.fits")]})
    )
    data = json.loads(workspace.manifest.read_text())

    def legacy(value):
        if isinstance(value, dict):
            return {k: legacy(v) for k, v in value.items() if k not in {"sha256", "ctime_ns"}}
        if isinstance(value, list):
            return [legacy(v) for v in value]
        return value

    data = legacy(data)
    for entry in data["runs"]:
        entry.pop("input_file", None)
        for output in entry.get("outputs", []):
            output.pop("mtime_ns", None)
    workspace.manifest.write_text(json.dumps(data))
    originals = data["runs"]
    drift(list(workspace.root.glob("*/*.fits")))
    refused = execute(config, archive, forbidden, forbidden, forbidden)
    assert refused.failures
    imported = CliRunner().invoke(
        cli.app, ["import-checksums", str(workspace.manifest), str(baseline)]
    )
    assert imported.exit_code == 0, imported.output
    resumed = execute(config, archive, forbidden, forbidden, forbidden)
    assert not resumed.failures
    assert resumed.branches[0].workflow.stage3.status == "skipped"
    after = json.loads(workspace.manifest.read_text())["runs"]
    by_id = {e["run_id"]: e for e in after}
    assert all(by_id[e["run_id"]] == e for e in originals)
    assert any(e.get("operation") == "checksum_baseline" and e["files"] for e in after)


def test_baseline_mismatch_never_writes_partial_proof(tmp_path):
    path = tmp_path / "raw.fits"
    path.write_bytes(b"science")
    reference = record_file(path)
    manifest = ManifestStore(tmp_path / "manifest.json", "Star")
    manifest.initialize()
    manifest.append({"operation": "stage1", "status": "success", "outputs": [reference]})
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"files": [reference]}))
    before = manifest.path.read_bytes()
    path.write_bytes(b"changed")
    with pytest.raises(JWSTReduxError, match="baseline mismatch"):
        manifest.import_checksums(baseline)
    assert manifest.path.read_bytes() == before


def test_hashing_retries_transient_metadata_changes_and_rejects_unstable_files(
    tmp_path, monkeypatch
):
    path = tmp_path / "science.fits"
    path.write_bytes(b"science")
    original = file_identity._digest
    calls = []

    def racing(path, signature):
        calls.append(signature)
        if len(calls) == 1:
            drift([path])
        return original(path, signature)

    monkeypatch.setattr(file_identity, "_digest", racing)
    reference = record_file(path)
    assert len(calls) == 2 and file_matches(reference)

    def unstable(path, signature):
        drift([path])
        return original(path, signature)

    monkeypatch.setattr(file_identity, "_digest", unstable)
    with pytest.raises(JWSTReduxError, match="kept changing"):
        record_file(path)


def test_rebased_official_association_reuses_only_identical_scientific_invocation(tmp_path):
    import hashlib

    from test_fixedslit_target_metadata import cal_inputs

    from jwst_redux.associations import generate_level3_associations

    paths = cal_inputs(tmp_path)
    (before,) = generate_level3_associations(paths, tmp_path / "old", "Asn_Lv3NRSFSS")
    (after,) = generate_level3_associations(paths, tmp_path / "new" / "nested", "Asn_Lv3NRSFSS")
    assert before.content_sha256 != after.content_sha256
    output = tmp_path / "spectrum.fits"
    output.write_bytes(b"spectrum")
    store = ManifestStore(tmp_path / "manifest.json", "Star")
    store.initialize()
    store.append(
        {
            "operation": "stage3",
            "status": "success",
            "association": before.record(),
            "outputs": [record_file(output)],
            "crds_context": "jwst_test.pmap",
        }
    )
    assert store.successful_entry(
        "stage3", {"association": after.record(), "crds_context": "jwst_test.pmap"}, {}
    )
    assert (
        store.successful_entry(
            "stage3", {"association": after.record(), "crds_context": "different.pmap"}, {}
        )
        is None
    )
    changed = json.loads(after.path.read_text())
    changed["version_id"] = "changed official invocation"
    after.path.write_text(json.dumps(changed))
    record = {
        **after.record(),
        "content_sha256": hashlib.sha256(after.path.read_bytes()).hexdigest(),
    }
    assert store.successful_entry("stage3", {"association": record}, {}) is None
    # A stale recorded hash never authorizes an altered document.
    assert store.successful_entry("stage3", {"association": after.record()}, {}) is None
