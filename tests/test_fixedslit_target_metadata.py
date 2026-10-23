"""Unnamed fixed-slit targets retain real identity and completed calibration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from astropy.io import fits
from astropy.table import MaskedColumn, Table
from test_fixedslit import Downloader, Spec3, _discovery, _header, _run

from jwst_redux.associations import generate_level3_associations
from jwst_redux.batch import prepare_batch
from jwst_redux.datasets import build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.client import _table_records
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure
from jwst_redux.stage1 import run_selected_through
from jwst_redux.workspace import Workspace


def cal_inputs(tmp_path, *, name="", count=3):
    paths = []
    for nod in range(1, count + 1):
        path = tmp_path / f"jw01128006001_0310c_{nod:05d}_nrs1_cal.fits"
        header = _header(path.name)
        if name is None:
            del header["TARGNAME"]
        else:
            header["TARGNAME"] = name
        header["TARGPROP"] = "Catalog target"
        header["PROP_RA"], header["PROP_DEC"] = 48.62, -4.25
        fits.PrimaryHDU(header=header).writeto(path)
        paths.append(path)
    return tuple(paths)


def identity(path):
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["", None])
def test_official_associations_accept_blank_or_absent_name_without_changing_fits(tmp_path, name):
    paths = cal_inputs(tmp_path, name=name)
    before = {p: identity(p) for p in paths}
    associations = generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")
    assert len(associations) == 1
    association = associations[0]
    data = json.loads(association.path.read_text())
    assert data["asn_rule"] == "Asn_Lv3NRSFSS"
    assert data["target"] == "t001"  # Pool-local ID, not the archive target number.
    assert len(data["products"][0]["members"]) == 3
    assert association.source["target_identity"]["TARGPROP"] == "Catalog target"
    from jwst.associations import load_asn

    load_asn(association.path.read_text(), validate=True)
    assert {p: identity(p) for p in paths} == before
    assert all(fits.getheader(p).get("TARGNAME") == name for p in paths)


def test_mixed_missing_and_present_names_use_consistent_real_proposal_identity(tmp_path):
    paths = cal_inputs(tmp_path)
    with fits.open(paths[0], mode="update") as hdus:
        hdus[0].header["TARGNAME"] = "Archive name"
    result = generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")
    assert len(result) == 1 and len(result[0].member_paths) == 3
    assert result[0].source["target_identity"]["TARGNAME"] == "Archive name"


@pytest.mark.parametrize(
    "key,value",
    [
        ("PROGRAM", "99999"),
        ("OBSERVTN", "007"),
        ("VISIT", "002"),
        ("TARGPROP", "Another target"),
        ("PROP_RA", 49.0),
        ("TARGETID", 2),
    ],
)
def test_unnamed_targets_never_mix_incompatible_identity(tmp_path, key, value):
    paths = cal_inputs(tmp_path)
    if key == "TARGETID":
        for p in paths:
            with fits.open(p, mode="update") as hdus:
                hdus[0].header[key] = 1
    with fits.open(paths[-1], mode="update") as hdus:
        hdus[0].header[key] = value
    with pytest.raises(PipelineExecutionError, match="target|program"):
        generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")


def test_missing_all_target_identifiers_fails_closed(tmp_path):
    paths = cal_inputs(tmp_path)
    for p in paths:
        with fits.open(p, mode="update") as hdus:
            del hdus[0].header["TARGPROP"]
    with pytest.raises(PipelineExecutionError, match="require consistent TARGPROP"):
        generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")


def test_positive_header_target_id_can_identify_unnamed_target(tmp_path):
    paths = cal_inputs(tmp_path)
    for p in paths:
        with fits.open(p, mode="update") as hdus:
            del hdus[0].header["TARGPROP"]
            hdus[0].header["TARGETID"] = 7
    result = generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")
    assert result[0].source["target_identity"]["TARGETID"] == "7"


def test_masked_archive_name_is_missing_and_proposal_name_is_preserved():
    from test_fixedslit import _records

    table = Table({key: [value] for key, value in _records()[-1].items()})
    table["targname"] = MaskedColumn([""], mask=[True])
    table["targprop"] = ["Catalog target"]
    row = _table_records(table)[0]
    assert row["targname"] is None
    exposure = normalize_exposure(row)
    assert exposure.target_name == "Catalog target"
    assert exposure.metadata["targname"] is None
    row["targname"] = "Existing archive name"
    assert normalize_exposure(row).target_name == "Existing archive name"


class UnnamedDownloader(Downloader):
    def download_product(self, uri, destination):
        result = super().download_product(uri, destination)
        if uri.endswith("_uncal.fits"):
            with fits.open(destination, mode="update") as hdus:
                hdus[0].header["TARGNAME"] = ""
                hdus[0].header["TARGPROP"] = "Catalog target"
        return result


def test_corrected_masked_name_reuses_legacy_workspace_and_stage2_products(tmp_path):
    config, original = _discovery(tmp_path)
    config = replace(
        config,
        qa_enabled=False,
        discovery=replace(
            config.discovery,
            query=replace(config.discovery.query, target=None, archive_target_names=()),
        ),
    )
    legacy = DiscoveryResult(
        build_science_datasets(tuple(replace(e, target_name="0.0") for e in original.exposures))
    )
    branch = prepare_batch(config, discoverer=lambda _: legacy).branches[0]
    from test_fixedslit import Detector1, Spec2

    stage2 = run_selected_through(
        branch.config,
        through="stage2",
        discoverer=lambda _: legacy,
        downloader=UnnamedDownloader(),
        detector1_pipeline=Detector1(),
        spec2_pipeline=Spec2(),
        context_resolver=lambda _: "jwst_test.pmap",
    )
    workspace = Workspace.for_config(branch.config)
    before = {
        p: identity(p)
        for stage in (workspace.raw, workspace.stage1, workspace.stage2)
        for p in stage.glob("*.fits")
    }
    corrected = DiscoveryResult(
        build_science_datasets(
            tuple(replace(e, target_name="Catalog target") for e in original.exposures)
        )
    )
    unrelated = DiscoveryResult(
        build_science_datasets(
            tuple(replace(e, target_name="Another target") for e in original.exposures)
        )
    )
    assert (
        prepare_batch(config, discoverer=lambda _: unrelated).branches[0].config.group_id
        != branch.config.group_id
    )
    prepared = prepare_batch(config, discoverer=lambda _: corrected)
    assert prepared.branches[0].config.group_id == branch.config.group_id

    def forbidden(*_args):
        pytest.fail("Completed Detector1/Spec2 was invoked")

    result = _run(
        config,
        corrected,
        detector=forbidden,
        spec2=forbidden,
        spec3=Spec3(),
        downloader=UnnamedDownloader(),
    )
    assert not result.failures
    workflow = result.branches[0].workflow
    assert workflow.stage3.status == "success"
    assert all(s.stage1.status == s.stage2.status == "skipped" for s in workflow.segments)
    assert {p: identity(p) for p in before} == before
    for a, b in zip(stage2.segments, workflow.segments, strict=True):
        assert b.stage2.manifest_entry["resumed_from_run_id"] == a.stage2.manifest_entry["run_id"]

    repeated = _run(
        config,
        corrected,
        detector=forbidden,
        spec2=forbidden,
        spec3=forbidden,
        downloader=UnnamedDownloader(),
    )
    assert not repeated.failures
    assert repeated.branches[0].workflow.stage3.status == "skipped"
    assert all(
        s.stage1.status == s.stage2.status == "skipped"
        for s in repeated.branches[0].workflow.segments
    )
    assert {p: identity(p) for p in before} == before


@pytest.mark.parametrize("target_id", [0, -1, "null"])
def test_placeholder_target_ids_do_not_establish_identity(tmp_path, target_id):
    paths = cal_inputs(tmp_path)
    for path in paths:
        with fits.open(path, mode="update") as hdus:
            del hdus[0].header["TARGPROP"]
            hdus[0].header["TARGETID"] = target_id
    with pytest.raises(PipelineExecutionError, match="require consistent TARGPROP"):
        generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")


def test_masked_values_remain_missing_without_losing_real_zeroes():
    table = Table(
        [
            MaskedColumn(name="text", data=["", "Star"], mask=[True, False]),
            MaskedColumn(name="integer", data=[0, 0], mask=[True, False]),
            MaskedColumn(name="boolean", data=[False, False], mask=[True, False]),
        ]
    )
    assert _table_records(table) == [
        {"text": None, "integer": None, "boolean": None},
        {"text": "Star", "integer": 0, "boolean": False},
    ]


def test_undefined_fits_name_remains_undefined_on_disk(tmp_path):
    paths = cal_inputs(tmp_path)
    for path in paths:
        with fits.open(path, mode="update") as hdus:
            hdus[0].header["TARGNAME"] = None
    before = {path: identity(path) for path in paths}
    result = generate_level3_associations(paths, tmp_path / "asn", "Asn_Lv3NRSFSS")
    assert len(result) == 1
    assert {path: identity(path) for path in paths} == before
    assert all(fits.getheader(path)["TARGNAME"] is None for path in paths)
