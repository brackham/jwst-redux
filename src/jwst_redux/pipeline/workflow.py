"""Manifest-backed execution shared by association pipeline stages."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..exceptions import JWSTReduxError, PipelineExecutionError
from ..provenance import ManifestStore, make_run_key, software_versions, utc_now
from .runner import PipelineCallable, pipeline_log_summary, run_spec2, run_stage3


@dataclass(frozen=True)
class RecordedResult:
    status: str
    outputs: tuple[Path, ...]
    elapsed_seconds: float
    log_path: Path
    manifest_entry: dict[str, Any]


def execute_association(
    manifest: ManifestStore,
    identity: dict[str, Any],
    association_path: Path,
    output_dir: Path,
    logs: Path,
    pipeline_name: str,
    operation: str,
    crds_context: str,
    overrides: dict[str, Any],
    *,
    overwrite: bool,
    pipeline: PipelineCallable | None = None,
    classify: Any = None,
) -> RecordedResult:
    """Resume intact matching success, or record an official association invocation."""
    run_id = str(uuid.uuid4())
    log_path = logs / f"{operation}-{association_path.stem}-{run_id[:8]}.log"
    base = {
        **identity,
        "pipeline_class": f"jwst.pipeline.{pipeline_name}",
        "explicit_parameter_overrides": overrides,
        "software": software_versions(),
        "crds_context": crds_context,
    }
    key = make_run_key(base)
    manifest.append(
        {
            **base,
            "run_id": run_id,
            "run_key": key,
            "operation": operation,
            "status": "running",
            "start_time": utc_now(),
            "end_time": None,
            "log_path": str(log_path),
            "input_path": str(association_path),
        }
    )
    previous = None if overwrite else manifest.successful_run(key)
    if previous is None and not overwrite:
        # A bookkeeping-only git commit must not trigger recalibration. Match
        # the complete scientific invocation and dependency identities while
        # retaining the original software/commit provenance on the source run.
        software_keys = (
            "jwst_redux_version",
            "python_version",
            "jwst_version",
            "astroquery_version",
        )
        previous = manifest.successful_entry(
            operation,
            {name: value for name, value in base.items() if name != "software"},
            {name: base["software"][name] for name in software_keys},
        )
    if previous is not None:
        log_path.write_text(
            f"jwst-redux: {pipeline_name} skipped; successful run {previous['run_id']} remains complete.\n"
        )
        manifest.update(
            run_id,
            {
                "status": "skipped",
                "end_time": utc_now(),
                "elapsed_processing_seconds": 0.0,
                "outputs": previous["outputs"],
                "resumed_from_run_id": previous["run_id"],
                "pipeline_configuration": previous.get("pipeline_configuration"),
            },
        )
        return RecordedResult(
            "skipped",
            tuple(Path(r["path"]) for r in previous["outputs"]),
            0.0,
            log_path,
            manifest.entry(run_id),
        )
    started = time.monotonic()
    try:
        if operation == "stage2":
            result = run_spec2(
                association_path, output_dir, log_path, crds_context, overrides, pipeline=pipeline
            )
        else:
            result = run_stage3(
                association_path,
                output_dir,
                log_path,
                crds_context,
                overrides,
                pipeline=pipeline,
                pipeline_name=pipeline_name,
            )
        records = [
            classify(p)
            if classify
            else {
                "path": str(p),
                "size_bytes": p.stat().st_size,
                "product_type": p.stem.rsplit("_", 1)[-1],
                "role": f"{operation}_output",
            }
            for p in result.outputs
        ]
        summary = pipeline_log_summary(log_path, pipeline_name)
        summary["pipeline_configuration"]["invocation"] = {
            "association_path": str(association_path),
            "working_directory": str(association_path.parent.resolve()),
            "output_dir": str(output_dir),
            "save_results": True,
            "explicit_parameter_overrides": overrides,
        }
        manifest.update(
            run_id,
            {
                "status": "success",
                "end_time": utc_now(),
                "outputs": records,
                "elapsed_processing_seconds": result.elapsed_seconds,
                **summary,
            },
        )
        return RecordedResult(
            "success", result.outputs, result.elapsed_seconds, log_path, manifest.entry(run_id)
        )
    except Exception as error:
        manifest.update(
            run_id,
            {
                "status": "failed",
                "end_time": utc_now(),
                "error": repr(error),
                "elapsed_processing_seconds": time.monotonic() - started,
                **pipeline_log_summary(log_path, pipeline_name),
            },
        )
        if isinstance(error, JWSTReduxError):
            raise
        raise PipelineExecutionError(f"{pipeline_name} failed: {error}") from error
