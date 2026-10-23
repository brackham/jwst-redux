"""Provenance checks for completed association groups with pruned intermediates."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .associations import LocalAssociation
from .config import WriteConfig
from .models import ReductionGroup, ScienceDataset, SelectedProduct
from .pipeline import workflow
from .provenance import ManifestStore, make_run_key


def group_checkpoint(config: WriteConfig, group: ReductionGroup) -> dict[str, Any]:
    """Identity available from fresh discovery, independent of retained FITS files."""
    software = workflow.software_versions()
    return {
        "group_id": config.group_id,
        "compatibility": dict(group.compatibility),
        "exposures": [e.exposure_id for e in group.exposures],
        "archive_inputs": [
            {"uri": p.uri, "filename": p.filename, "size_bytes": p.size_bytes}
            for p in group.products
        ],
        "archive_associations": sorted(
            {
                (p.uri, p.filename, p.size_bytes)
                for e in group.exposures
                for p in e.association_products
            },
            key=lambda p: (p[0], p[1]),
        ),
        "overrides": {
            name: config.overrides_for(name)
            for name in ("Detector1Pipeline", "Spec2Pipeline", "Spec3Pipeline")
        },
        "requested_crds_context": config.crds_context,
        "software": {
            key: software[key]
            for key in (
                "jwst_redux_version",
                "python_version",
                "jwst_version",
                "astroquery_version",
            )
        },
    }


def checkpoint_key(config: WriteConfig, group: ReductionGroup) -> str:
    return make_run_key(group_checkpoint(config, group))


def completed_group(
    manifest: ManifestStore,
    config: WriteConfig,
    group: ReductionGroup,
    *,
    crds_context: str | None = None,
    associations: tuple[LocalAssociation, ...] | None = None,
) -> dict[str, Any] | None:
    """Require intact endpoint products and unchanged local/archived associations.

    Runtime callers additionally compare freshly fetched archive ASN contents and
    the resolved CRDS context. Read-only plans can only assess local provenance.
    """
    required: dict[str, Any] = {"checkpoint_key": checkpoint_key(config, group)}
    if crds_context is not None:
        required["crds_context"] = crds_context
    if associations is not None:
        required["level2_association_hashes"] = [a.content_sha256 for a in associations]
    previous = manifest.successful_entry("group", required, {})
    if previous is None:
        previous = _legacy_completed_group(manifest, config, group, crds_context, associations)
    if previous is None:
        return None
    endpoint = manifest.entry(previous.get("endpoint_run_id", ""))
    if (
        endpoint is None
        or endpoint.get("operation") != "stage3"
        or endpoint.get("status") != "success"
        or endpoint.get("outputs") != previous["outputs"]
        or not previous.get("associations")
    ):
        return None
    for record in previous.get("associations", []):
        path = Path(record["path"])
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != record["content_sha256"]
        ):
            return None
    return previous


def _legacy_completed_group(
    manifest: ManifestStore,
    config: WriteConfig,
    group: ReductionGroup,
    crds_context: str | None,
    associations: tuple[LocalAssociation, ...] | None,
) -> dict[str, Any] | None:
    """Prove an older pruned endpoint from its complete recorded dependency chain.

    Older fixed-slit manifests have no group checkpoint. Only accept one with
    matching group membership, every Stage-1/2 dependency and override, a final
    retention receipt, and all of the original association documents.
    """
    from .stage1 import _base_entry

    software = group_checkpoint(config, group)["software"]
    members = [e.exposure_id for e in group.exposures]
    archived = {p.uri for e in group.exposures for p in e.association_products}
    for old_group in reversed(manifest.entries()):
        if (
            old_group.get("operation") != "group"
            or old_group.get("status") != "success"
            or "checkpoint_key" in old_group
            or old_group.get("group_id") != config.group_id
            or old_group.get("exposure_identifiers") != members
            or old_group.get("compatibility") != dict(group.compatibility)
            or old_group.get("requested_crds_context") != config.crds_context
            or (crds_context is not None and old_group.get("crds_context") != crds_context)
        ):
            continue
        identity = {
            key: old_group[key]
            for key in ("group_id", "compatibility", "exposure_identifiers", "crds_context")
        }
        endpoint = manifest.successful_entry(
            "stage3",
            {
                **identity,
                "explicit_parameter_overrides": config.overrides_for("Spec3Pipeline"),
            },
            software,
        )
        if endpoint is None:
            continue
        stage2 = [
            manifest.entry(run_id) for run_id in endpoint["association"]["upstream_stage2_run_ids"]
        ]
        if any(entry is None for entry in stage2):
            continue
        expected = {}
        for product in group.products:
            exposure = next(e for e in group.exposures if e.exposure_id == product.exposure_id)
            expected[product.filename] = (exposure, product)
        found = set()
        valid = True
        for entry in stage2:
            pair = expected.get(entry.get("archive_filename"))
            if pair is None or entry["association"].get("source", {}).get("uri") not in archived:
                valid = False
                break
            exposure, product = pair
            # _base_entry needs only dataset identity, also preserved by the old group.
            dataset = ScienceDataset(
                group.dataset_id, tuple(old_group["scientific_dataset"].items()), ()
            )
            selected = SelectedProduct(dataset, exposure, product)
            base2 = _base_entry(
                config,
                selected,
                pipeline_class="jwst.pipeline.Spec2Pipeline",
                parameter_overrides=config.overrides_for("Spec2Pipeline"),
            )
            if not _legacy_entry_matches(entry, base2, identity["crds_context"], software):
                valid = False
                break
            for run_id in entry["upstream_stage1_run_ids"]:
                upstream = manifest.entry(run_id)
                pair1 = expected.get((upstream or {}).get("archive_filename"))
                if pair1 is None:
                    valid = False
                    break
                base1 = _base_entry(config, SelectedProduct(dataset, *pair1))
                if not _legacy_entry_matches(upstream, base1, identity["crds_context"], software):
                    valid = False
                    break
            found.add(product.filename)
        if not valid or found != set(expected):
            continue
        hashes = [e["association"]["content_sha256"] for e in stage2]
        if associations is not None and sorted(hashes) != sorted(
            a.content_sha256 for a in associations
        ):
            continue
        outputs = endpoint["outputs"]
        if not any(
            entry.get("operation") == "retention"
            and entry.get("status") == "success"
            and entry.get("retention_policy") == "final"
            and entry.get("endpoint") == "stage3"
            and entry.get("endpoint_output_paths") == [p["path"] for p in outputs]
            for entry in manifest.entries()
        ):
            continue
        return {
            **old_group,
            "outputs": outputs,
            "endpoint_run_id": endpoint["run_id"],
            "retention_policy": "final",
            "retention_status": "success",
            "associations": [*(e["association"] for e in stage2), endpoint["association"]],
        }
    return None


def _legacy_entry_matches(
    entry: dict[str, Any], base: dict[str, Any], context: str, software: dict[str, Any]
) -> bool:
    return (
        entry.get("status") == "success"
        and entry.get("crds_context") == context
        and all(entry.get(key) == value for key, value in base.items() if key != "selection")
        and all(entry.get("software", {}).get(key) == value for key, value in software.items())
    )
