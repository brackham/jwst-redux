"""Coordinated execution for modes with multi-exposure Level-2 associations."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .associations import generate_level3_associations, resolve_level2_associations
from .config import ExposureSelectionConfig, WriteConfig
from .exceptions import PipelineExecutionError
from .group_resume import checkpoint_key, completed_group
from .models import ReductionGroup, SelectedExposure, SelectedProduct
from .modes import observing_mode_for
from .pipeline.runner import resolve_crds_context
from .pipeline.workflow import execute_association
from .planning.associations import build_compatible_groups
from .provenance import ManifestStore, software_versions, utc_now
from .retention import apply_retention
from .workspace import Workspace


def group_workspace_id(group: ReductionGroup) -> str:
    digest = hashlib.sha256(repr(group.compatibility).encode()).hexdigest()[:16]
    return f"{group.dataset_id}-{digest}"


def run_group_through(
    config: WriteConfig,
    discovery: Any,
    *,
    through: str,
    overwrite: bool,
    downloader: Any,
    detector1_pipeline: Any,
    spec2_pipeline: Any,
    stage3_pipeline: Any,
    context_resolver: Any,
):
    """Process the complete compatible group containing the selected exposure.

    All Stage-1 products precede association generation and all Stage-2 runs
    precede Stage 3 or retention. This keeps nod background dependencies intact.
    """

    matches = [
        (dataset, group)
        for dataset in discovery.datasets
        for group in build_compatible_groups(dataset)
        if any(e.exposure_id == config.selection.exposure_id for e in group.exposures)
        and all(
            dict(dataset.identity).get(k) == v
            for k, v in {
                "program_id": config.selection.program_id,
                "observation_id": config.selection.observation_id,
                "visit_number": config.selection.visit_number,
            }.items()
        )
    ]
    if len(matches) != 1:
        raise PipelineExecutionError(
            "Selection must identify exactly one compatible association group."
        )
    dataset, group = matches[0]
    if config.selection.detector is not None:
        raise PipelineExecutionError(
            "Group spectroscopy selects both useful detectors; omit selection.detector."
        )
    config = replace(config, group_id=group_workspace_id(group))
    workspace = Workspace.for_config(config)
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.label)
    manifest.initialize()
    identity = {
        "scientific_target": config.discovery.query.target or group.exposures[0].target_name,
        "scientific_dataset": dict(dataset.identity),
        "group_id": config.group_id,
        "compatibility": dict(group.compatibility),
        "exposure_identifiers": [e.exposure_id for e in group.exposures],
        "requested_crds_context": config.crds_context,
    }
    group_run_id = str(uuid.uuid4())
    manifest.append(
        {
            **identity,
            "run_id": group_run_id,
            "operation": "group",
            "status": "running",
            "start_time": utc_now(),
            "end_time": None,
            "software": software_versions(),
            "archive_inputs": [
                {"uri": p.uri, "filename": p.filename, "size_bytes": p.size_bytes}
                for p in group.products
            ],
        }
    )
    try:
        return _execute_group(
            config,
            group,
            dataset,
            workspace,
            manifest,
            identity,
            through=through,
            overwrite=overwrite,
            downloader=downloader,
            detector1_pipeline=detector1_pipeline,
            spec2_pipeline=spec2_pipeline,
            stage3_pipeline=stage3_pipeline,
            context_resolver=context_resolver,
            group_run_id=group_run_id,
        )
    except Exception as error:
        manifest.update(
            group_run_id, {"status": "failed", "end_time": utc_now(), "error": repr(error)}
        )
        raise


def _execute_group(
    config,
    group,
    dataset,
    workspace,
    manifest,
    identity,
    *,
    through,
    overwrite,
    downloader,
    detector1_pipeline,
    spec2_pipeline,
    stage3_pipeline,
    context_resolver,
    group_run_id,
):
    from .stage1 import (
        SegmentWorkflowResult,
        Stage2WorkflowResult,
        Stage3Readiness,
        Stage3WorkflowResult,
        ThroughWorkflowResult,
        _base_entry,
        _validate_nirspec_file_metadata,
        run_selected_stage1,
    )

    mode = observing_mode_for(group.exposures[0])
    associations = ()
    if through in {"stage2", "stage3"}:
        # Validate all archived dependencies before downloading science or resolving CRDS.
        expected_rates = tuple(
            workspace.stage1 / p.filename.replace("_uncal.fits", "_rate.fits")
            for p in group.products
        )
        archive_associations = tuple(p for e in group.exposures for p in e.association_products)
        associations = resolve_level2_associations(
            archive_associations,
            expected_rates,
            workspace.stage2 / "associations",
            downloader=downloader,
        )
    # Resolve once and share the same context across the entire group.
    context = (context_resolver or resolve_crds_context)(config.crds_context)
    manifest.update(group_run_id, {"crds_context": context})
    if through == "stage3" and config.retention == "final" and not (overwrite or config.overwrite):
        previous = completed_group(
            manifest, config, group, crds_context=context, associations=associations
        )
        if (
            previous is not None
            and previous.get("retention_policy") == "final"
            and previous.get("retention_status") == "success"
        ):
            return _resume_endpoint(manifest, previous, group_run_id, dataset, group, workspace)
    stage1_results = []
    for product in group.products:
        exposure = next(e for e in group.exposures if e.exposure_id == product.exposure_id)
        selected = SelectedProduct(dataset, exposure, product)
        stage1_results.append(
            run_selected_stage1(
                _member_config(config, exposure),
                overwrite=overwrite,
                downloader=downloader,
                pipeline=detector1_pipeline,
                context_resolver=lambda _: context,
                _selected=selected,
            )
        )
    segments = [SegmentWorkflowResult(s) for s in stage1_results]
    stage2_results = []
    stage3 = None
    readiness = None
    stage_entries = {
        "stage1": tuple(s.manifest_entry for s in stage1_results),
        "stage2": (),
        "stage3": (),
    }
    if config.qa_enabled:
        manifest.append(
            {
                **identity,
                "operation": "qa",
                "status": "skipped",
                "reason": "Time-series QA is unavailable for non-TSO fixed-slit spectroscopy.",
            }
        )
    if through in {"stage2", "stage3"}:
        rates = tuple(
            next(p for p in s.outputs if p.name.endswith(mode.stage2_input_suffix + ".fits"))
            for s in stage1_results
        )
        for rate, stage1 in zip(rates, stage1_results, strict=True):
            _validate_nirspec_file_metadata(
                rate, stage1.selected.exposure, stage1.selected.product.detector
            )
        upstream = {
            p: s.manifest_entry.get("resumed_from_run_id", s.manifest_entry["run_id"])
            for p, s in zip(rates, stage1_results, strict=True)
        }
        by_rate = dict(zip(rates, stage1_results, strict=True))
        for association in associations:
            record = association.record()
            science = next(m for m in record["members"] if m["exptype"] == "science")
            rate = (association.path.parent / science["expname"]).resolve()
            parent = by_rate[rate]
            base = _base_entry(
                _member_config(config, parent.selected.exposure),
                parent.selected,
                pipeline_class="jwst.pipeline.Spec2Pipeline",
                parameter_overrides=config.overrides_for("Spec2Pipeline"),
            )
            result = execute_association(
                manifest,
                {
                    **{k: v for k, v in identity.items() if k != "exposure_identifiers"},
                    **base,
                    "association": record,
                    "upstream_stage1_run_ids": [upstream[p] for p in association.member_paths],
                    "input_product_type": mode.stage2_input_suffix.removeprefix("_"),
                },
                association.path,
                workspace.stage2,
                workspace.logs,
                "Spec2Pipeline",
                "stage2",
                context,
                config.overrides_for("Spec2Pipeline"),
                overwrite=overwrite or config.overwrite,
                pipeline=spec2_pipeline,
            )
            stage2_results.append(
                Stage2WorkflowResult(
                    parent.selected,
                    result.status,
                    association.path,
                    result.outputs,
                    result.elapsed_seconds,
                    context,
                    result.log_path,
                    result.manifest_entry,
                )
            )
        by_filename = {r.selected.product.filename: r for r in stage2_results}
        segments = [
            SegmentWorkflowResult(s, by_filename[s.selected.product.filename])
            for s in stage1_results
        ]
        cals = tuple(
            next(p for p in r.outputs if p.name.endswith(mode.stage3_input_suffix + ".fits"))
            for r in stage2_results
        )
        for cal, stage2 in zip(cals, stage2_results, strict=True):
            _validate_nirspec_file_metadata(
                cal, stage2.selected.exposure, stage2.selected.product.detector
            )
        run_ids = tuple(
            r.manifest_entry.get("resumed_from_run_id", r.manifest_entry["run_id"])
            for r in stage2_results
        )
        representative = SelectedExposure(dataset, group.exposures[0], group.products)
        readiness = Stage3Readiness(representative, cals, run_ids)
        stage_entries["stage2"] = tuple(r.manifest_entry for r in stage2_results)
        if through == "stage3":
            associations3 = generate_level3_associations(
                cals, workspace.associations, mode.stage3_association_rule
            )
            if len(associations3) != 1:
                raise PipelineExecutionError(
                    "Compatible group did not resolve to one official Level-3 association."
                )
            association = associations3[0]
            result = execute_association(
                manifest,
                {
                    **identity,
                    "association": {
                        **association.record(),
                        "upstream_stage2_run_ids": list(run_ids),
                    },
                },
                association.path,
                workspace.stage3,
                workspace.logs,
                mode.stage3_class,
                "stage3",
                context,
                config.overrides_for(mode.stage3_class),
                overwrite=overwrite or config.overwrite,
                pipeline=stage3_pipeline,
            )
            stage3 = Stage3WorkflowResult(
                readiness,
                association,
                result.status,
                result.outputs,
                result.elapsed_seconds,
                context,
                result.log_path,
                result.manifest_entry,
            )
            stage_entries["stage3"] = (result.manifest_entry,)
    retention = apply_retention(config, through, stage_entries, qa_succeeded=True)
    manifest.update(
        group_run_id,
        {
            "status": "success",
            "end_time": utc_now(),
            "crds_context": context,
            "retention_policy": config.retention,
            "retention_status": retention.status,
            **(
                {
                    "checkpoint_key": checkpoint_key(config, group),
                    "level2_association_hashes": [a.content_sha256 for a in associations],
                    "endpoint_run_id": stage3.manifest_entry.get(
                        "resumed_from_run_id", stage3.manifest_entry["run_id"]
                    ),
                    "outputs": stage3.manifest_entry["outputs"],
                    "associations": [
                        *(r.manifest_entry["association"] for r in stage2_results),
                        stage3.manifest_entry["association"],
                    ],
                }
                if stage3 is not None
                else {}
            ),
        },
    )
    return ThroughWorkflowResult(tuple(segments), readiness, stage3, retention)


def _member_config(config, exposure):
    """Keep per-file provenance stable when a group's representative changes."""
    return replace(
        config,
        selection=ExposureSelectionConfig(
            exposure.program_id,
            exposure.observation_id,
            exposure.visit_number,
            exposure.exposure_id,
        ),
    )


def _resume_endpoint(manifest, previous, run_id, dataset, group, workspace):
    from .associations import LocalAssociation
    from .stage1 import Stage3Readiness, Stage3WorkflowResult, ThroughWorkflowResult

    entry = manifest.entry(previous["endpoint_run_id"])
    record = entry["association"]
    members = tuple(Path(p["path"]) for p in record["member_files"])
    association = LocalAssociation(
        Path(record["path"]),
        record["content_sha256"],
        members,
        record["product_name"],
        True,
        record["official_rule"],
        record.get("source", {}),
    )
    readiness = Stage3Readiness(
        SelectedExposure(dataset, group.exposures[0], group.products),
        members,
        tuple(record["upstream_stage2_run_ids"]),
    )
    log = workspace.logs / f"group-resume-{run_id[:8]}.log"
    log.write_text(
        f"jwst-redux: Spec3Pipeline skipped; completed group {previous['run_id']} remains valid.\n"
        "Upstream products are not required for this completed endpoint.\n"
    )
    manifest.update(
        run_id,
        {
            "status": "skipped",
            "end_time": utc_now(),
            "resumed_from_run_id": previous["run_id"],
            "outputs": previous["outputs"],
            "log_path": str(log),
        },
    )
    resumed = {
        **entry,
        "run_id": str(uuid.uuid4()),
        "status": "skipped",
        "start_time": utc_now(),
        "end_time": utc_now(),
        "elapsed_processing_seconds": 0.0,
        "log_path": str(log),
        "resumed_from_run_id": entry["run_id"],
    }
    manifest.append(resumed)
    stage3 = Stage3WorkflowResult(
        readiness,
        association,
        "skipped",
        tuple(Path(p["path"]) for p in entry["outputs"]),
        0.0,
        entry["crds_context"],
        log,
        resumed,
    )
    return ThroughWorkflowResult((), readiness, stage3)
