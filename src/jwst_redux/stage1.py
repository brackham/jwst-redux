"""Single-product download and official pipeline orchestration."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import WriteConfig
from .exceptions import JWSTReduxError, PipelineExecutionError
from .mast.download import DownloadResult, ProductDownloader, ensure_downloaded
from .mast.query import DiscoveryResult, discover
from .models import SelectedProduct
from .pipeline.runner import (
    PipelineCallable,
    existing_stage1_outputs,
    expected_stage1_outputs,
    expected_stage2_outputs,
    pipeline_log_summary,
    resolve_crds_context,
    run_detector1,
    run_spec2,
)
from .provenance import ManifestStore, make_run_key, software_versions, utc_now
from .selection import select_stage1_product
from .workspace import Workspace

Discoverer = Callable[[Any], DiscoveryResult]
ContextResolver = Callable[[str], str]


@dataclass(frozen=True)
class DownloadWorkflowResult:
    selected: SelectedProduct
    download: DownloadResult
    manifest_entry: dict[str, Any]


@dataclass(frozen=True)
class Stage1WorkflowResult:
    selected: SelectedProduct
    download: DownloadResult
    status: str
    outputs: tuple[Path, ...]
    elapsed_seconds: float
    crds_context: str
    log_path: Path
    manifest_entry: dict[str, Any]


@dataclass(frozen=True)
class Stage2WorkflowResult:
    selected: SelectedProduct
    status: str
    input_path: Path
    outputs: tuple[Path, ...]
    elapsed_seconds: float
    crds_context: str
    log_path: Path
    manifest_entry: dict[str, Any]


@dataclass(frozen=True)
class ThroughWorkflowResult:
    stage1: Stage1WorkflowResult
    stage2: Stage2WorkflowResult | None = None


def download_selected(
    config: WriteConfig,
    *,
    overwrite: bool = False,
    discoverer: Discoverer | None = None,
    downloader: ProductDownloader | None = None,
) -> DownloadWorkflowResult:
    """Discover through the domain model and download exactly the configured product."""
    selected = _discover_selected(config, discoverer)
    workspace = Workspace(config.discovery.output_root.resolve())
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    manifest.initialize()
    run_id = str(uuid.uuid4())
    started = utc_now()
    software = software_versions()
    entry = {
        **_base_entry(config, selected),
        "run_id": run_id,
        "operation": "download",
        "status": "running",
        "start_time": started,
        "end_time": None,
        "software": software,
        "requested_crds_context": config.crds_context,
    }
    manifest.append(entry)
    try:
        result = ensure_downloaded(
            selected.product,
            workspace.raw,
            overwrite=overwrite or config.overwrite,
            downloader=downloader,
        )
        manifest.update(
            run_id,
            {
                "status": "success",
                "end_time": utc_now(),
                "download": _download_record(result),
                "input_path": str(result.path),
            },
        )
    except Exception as error:
        manifest.update(
            run_id,
            {"status": "failed", "end_time": utc_now(), "error": repr(error)},
        )
        raise
    return DownloadWorkflowResult(selected, result, manifest.entries()[-1])


def run_selected_stage1(
    config: WriteConfig,
    *,
    overwrite: bool = False,
    discoverer: Discoverer | None = None,
    downloader: ProductDownloader | None = None,
    pipeline: PipelineCallable | None = None,
    context_resolver: ContextResolver | None = None,
) -> Stage1WorkflowResult:
    """Download and run Detector1 for exactly the configured hierarchy selection."""
    selected = _discover_selected(config, discoverer)
    workspace = Workspace(config.discovery.output_root.resolve())
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    manifest.initialize()

    run_id = str(uuid.uuid4())
    log_path = workspace.logs / f"stage1-{selected.product.filename[:-5]}-{run_id[:8]}.log"
    software = software_versions()
    manifest.append(
        {
            **_base_entry(config, selected),
            "run_id": run_id,
            "operation": "stage1",
            "status": "running",
            "start_time": utc_now(),
            "end_time": None,
            "log_path": str(log_path),
            "software": software,
            "requested_crds_context": config.crds_context,
        }
    )

    pipeline_started: float | None = None
    try:
        download = ensure_downloaded(
            selected.product,
            workspace.raw,
            overwrite=overwrite or config.overwrite,
            downloader=downloader,
        )
        crds_context = (context_resolver or resolve_crds_context)(config.crds_context)
        outputs = expected_stage1_outputs(download.path, workspace.stage1)
        run_key = make_run_key(
            _resume_identity(config, selected, download.path, outputs, software, crds_context)
        )
        manifest.update(
            run_id,
            {
                "run_key": run_key,
                "software": software,
                "crds_context": crds_context,
                "download": _download_record(download),
                "input_path": str(download.path),
            },
        )

        previous = None if overwrite or config.overwrite else manifest.successful_run(run_key)
        if previous is None and not (overwrite or config.overwrite):
            previous = manifest.successful_entry(
                "stage1",
                {
                    **_base_entry(config, selected),
                    "crds_context": crds_context,
                    "input_path": str(download.path),
                },
                {
                    key: software[key]
                    for key in ("jwst_redux_version", "python_version", "jwst_version")
                },
            )
        if previous is not None:
            resumed_outputs = existing_stage1_outputs(download.path, workspace.stage1)
            output_records = [_file_record(path) for path in resumed_outputs]
            if output_records != previous["outputs"]:
                manifest.update(previous["run_id"], {"outputs": output_records})
            log_path.write_text(
                "jwst-redux: Detector1Pipeline skipped; matching successful manifest run "
                f"{previous['run_id']} remains complete.\n",
                encoding="utf-8",
            )
            manifest.update(
                run_id,
                {
                    "status": "skipped",
                    "end_time": utc_now(),
                    "elapsed_processing_seconds": 0.0,
                    "outputs": output_records,
                    "resumed_from_run_id": previous["run_id"],
                },
            )
            return Stage1WorkflowResult(
                selected=selected,
                download=download,
                status="skipped",
                outputs=resumed_outputs,
                elapsed_seconds=0.0,
                crds_context=crds_context,
                log_path=log_path,
                manifest_entry=manifest.entries()[-1],
            )

        pipeline_started = time.monotonic()
        stage1 = run_detector1(
            download.path,
            workspace.stage1,
            log_path,
            crds_context,
            config.parameter_overrides,
            pipeline=pipeline,
        )
        output_records = [_file_record(path) for path in stage1.outputs]
        manifest.update(
            run_id,
            {
                "status": "success",
                "end_time": utc_now(),
                "elapsed_processing_seconds": stage1.elapsed_seconds,
                "outputs": output_records,
            },
        )
        return Stage1WorkflowResult(
            selected=selected,
            download=download,
            status="success",
            outputs=stage1.outputs,
            elapsed_seconds=stage1.elapsed_seconds,
            crds_context=crds_context,
            log_path=log_path,
            manifest_entry=manifest.entries()[-1],
        )
    except Exception as error:
        elapsed = 0.0 if pipeline_started is None else time.monotonic() - pipeline_started
        manifest.update(
            run_id,
            {
                "status": "failed",
                "end_time": utc_now(),
                "elapsed_processing_seconds": elapsed,
                "error": repr(error),
            },
        )
        if isinstance(error, JWSTReduxError):
            raise
        raise PipelineExecutionError(f"Detector1Pipeline failed: {error}") from error


def run_selected_through(
    config: WriteConfig,
    *,
    through: str = "stage1",
    overwrite: bool = False,
    discoverer: Discoverer | None = None,
    downloader: ProductDownloader | None = None,
    detector1_pipeline: PipelineCallable | None = None,
    spec2_pipeline: PipelineCallable | None = None,
    context_resolver: ContextResolver | None = None,
) -> ThroughWorkflowResult:
    """Run the selected product through the requested official pipeline stage."""
    if through not in {"stage1", "stage2"}:
        raise PipelineExecutionError(f"Unsupported --through stage: {through}")
    stage1 = run_selected_stage1(
        config,
        overwrite=overwrite,
        discoverer=discoverer,
        downloader=downloader,
        pipeline=detector1_pipeline,
        context_resolver=context_resolver,
    )
    if through == "stage1":
        return ThroughWorkflowResult(stage1=stage1)
    stage2 = _run_selected_stage2(
        config,
        stage1,
        overwrite=overwrite,
        pipeline=spec2_pipeline,
    )
    return ThroughWorkflowResult(stage1=stage1, stage2=stage2)


def resolve_stage1_rateints(manifest_entry: dict[str, Any]) -> Path:
    """Resolve the intact Stage 1 integration product recorded by the manifest."""
    if manifest_entry.get("operation") != "stage1" or manifest_entry.get("status") != "success":
        raise PipelineExecutionError("Stage 2 requires a successful Stage 1 manifest entry.")
    records = manifest_entry.get("outputs")
    if not isinstance(records, list):
        raise PipelineExecutionError("Successful Stage 1 manifest entry has no output records.")
    candidates = [
        record
        for record in records
        if isinstance(record, dict)
        and (
            record.get("product_type") == "rateints"
            or str(record.get("path", "")).endswith("_rateints.fits")
        )
    ]
    if len(candidates) != 1:
        raise PipelineExecutionError(
            "Successful Stage 1 manifest entry must record exactly one _rateints product."
        )
    record = candidates[0]
    path = Path(str(record["path"]))
    size = record.get("size_bytes")
    if (
        not path.is_file()
        or not isinstance(size, int)
        or size <= 0
        or path.stat().st_size != size
    ):
        raise PipelineExecutionError(f"Recorded Stage 1 input is missing or changed: {path}")
    return path


def _run_selected_stage2(
    config: WriteConfig,
    stage1: Stage1WorkflowResult,
    *,
    overwrite: bool,
    pipeline: PipelineCallable | None,
) -> Stage2WorkflowResult:
    workspace = Workspace(config.discovery.output_root.resolve())
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    resumed_from = stage1.manifest_entry.get("resumed_from_run_id")
    if isinstance(resumed_from, str):
        successful_stage1 = manifest.entry(resumed_from)
    else:
        stage1_run_key = stage1.manifest_entry.get("run_key")
        successful_stage1 = (
            manifest.successful_run(stage1_run_key)
            if isinstance(stage1_run_key, str)
            else None
        )
    if successful_stage1 is None:
        raise PipelineExecutionError("No intact successful Stage 1 manifest run is available.")
    input_path = resolve_stage1_rateints(successful_stage1)
    outputs = expected_stage2_outputs(input_path, workspace.stage2)
    software = software_versions()
    crds_context = stage1.crds_context
    run_key = make_run_key(
        {
            **_base_entry(
                config,
                stage1.selected,
                pipeline_class="jwst.pipeline.Spec2Pipeline",
                parameter_overrides=config.spec2_parameter_overrides,
            ),
            "software": software,
            "crds_context": crds_context,
            "upstream_stage1_run_id": successful_stage1["run_id"],
            "input_path": str(input_path),
            "input_size_bytes": input_path.stat().st_size,
            "output_paths": [str(path) for path in outputs],
        }
    )
    run_id = str(uuid.uuid4())
    log_path = workspace.logs / f"stage2-{input_path.name[:-5]}-{run_id[:8]}.log"
    manifest.append(
        {
            **_base_entry(
                config,
                stage1.selected,
                pipeline_class="jwst.pipeline.Spec2Pipeline",
                parameter_overrides=config.spec2_parameter_overrides,
            ),
            "run_id": run_id,
            "run_key": run_key,
            "operation": "stage2",
            "status": "running",
            "start_time": utc_now(),
            "end_time": None,
            "log_path": str(log_path),
            "software": software,
            "requested_crds_context": config.crds_context,
            "crds_context": crds_context,
            "upstream_stage1_run_id": successful_stage1["run_id"],
            "input_path": str(input_path),
            "input_size_bytes": input_path.stat().st_size,
            "input_product_type": "rateints",
        }
    )

    pipeline_started: float | None = None
    try:
        previous = None if overwrite or config.overwrite else manifest.successful_run(run_key)
        if previous is None and not (overwrite or config.overwrite):
            previous = manifest.successful_entry(
                "stage2",
                {
                    **_base_entry(
                        config,
                        stage1.selected,
                        pipeline_class="jwst.pipeline.Spec2Pipeline",
                        parameter_overrides=config.spec2_parameter_overrides,
                    ),
                    "crds_context": crds_context,
                    "upstream_stage1_run_id": successful_stage1["run_id"],
                    "input_path": str(input_path),
                    "input_size_bytes": input_path.stat().st_size,
                },
                {
                    key: software[key]
                    for key in ("jwst_redux_version", "python_version", "jwst_version")
                },
            )
        if previous is not None:
            output_records = previous["outputs"]
            log_path.write_text(
                "jwst-redux: Spec2Pipeline skipped; matching successful manifest run "
                f"{previous['run_id']} remains complete.\n",
                encoding="utf-8",
            )
            manifest.update(
                run_id,
                {
                    "status": "skipped",
                    "end_time": utc_now(),
                    "elapsed_processing_seconds": 0.0,
                    "outputs": output_records,
                    "resumed_from_run_id": previous["run_id"],
                    "pipeline_configuration": previous.get("pipeline_configuration"),
                    "pipeline_messages": {
                        "warning_count": 0,
                        "error_count": 0,
                        "warnings": [],
                        "errors": [],
                    },
                },
            )
            return Stage2WorkflowResult(
                selected=stage1.selected,
                status="skipped",
                input_path=input_path,
                outputs=tuple(Path(record["path"]) for record in output_records),
                elapsed_seconds=0.0,
                crds_context=crds_context,
                log_path=log_path,
                manifest_entry=manifest.entries()[-1],
            )

        pipeline_started = time.monotonic()
        stage2 = run_spec2(
            input_path,
            workspace.stage2,
            log_path,
            crds_context,
            config.spec2_parameter_overrides,
            pipeline=pipeline,
        )
        provenance = _pipeline_provenance(
            log_path,
            "Spec2Pipeline",
            workspace.stage2,
            config.spec2_parameter_overrides,
        )
        output_records = [_stage2_file_record(path) for path in stage2.outputs]
        manifest.update(
            run_id,
            {
                "status": "success",
                "end_time": utc_now(),
                "elapsed_processing_seconds": stage2.elapsed_seconds,
                "outputs": output_records,
                **provenance,
            },
        )
        return Stage2WorkflowResult(
            selected=stage1.selected,
            status="success",
            input_path=input_path,
            outputs=stage2.outputs,
            elapsed_seconds=stage2.elapsed_seconds,
            crds_context=crds_context,
            log_path=log_path,
            manifest_entry=manifest.entries()[-1],
        )
    except Exception as error:
        elapsed = 0.0 if pipeline_started is None else time.monotonic() - pipeline_started
        manifest.update(
            run_id,
            {
                "status": "failed",
                "end_time": utc_now(),
                "elapsed_processing_seconds": elapsed,
                "error": repr(error),
                **_pipeline_provenance(
                    log_path,
                    "Spec2Pipeline",
                    workspace.stage2,
                    config.spec2_parameter_overrides,
                ),
            },
        )
        if isinstance(error, JWSTReduxError):
            raise
        raise PipelineExecutionError(f"Spec2Pipeline failed: {error}") from error


def _discover_selected(
    config: WriteConfig, discoverer: Discoverer | None
) -> SelectedProduct:
    result = (discoverer or discover)(config.discovery)
    return select_stage1_product(result.datasets, config.selection)


def _base_entry(
    config: WriteConfig,
    selected: SelectedProduct,
    *,
    pipeline_class: str = "jwst.pipeline.Detector1Pipeline",
    parameter_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exposure = selected.exposure
    product = selected.product
    return {
        "scientific_target": config.discovery.query.target,
        "scientific_dataset": dict(selected.dataset.identity),
        "exposure_identifier": exposure.exposure_id,
        "segment_number": product.segment_number,
        "archive_dataset_id": exposure.archive_id,
        "mast_product_uri": product.uri,
        "archive_filename": product.filename,
        "expected_raw_size_bytes": product.size_bytes,
        "pipeline_class": pipeline_class,
        "explicit_parameter_overrides": (
            config.parameter_overrides
            if parameter_overrides is None
            else parameter_overrides
        ),
    }


def _resume_identity(
    config: WriteConfig,
    selected: SelectedProduct,
    input_path: Path,
    outputs: tuple[Path, ...],
    software: dict[str, Any],
    crds_context: str,
) -> dict[str, Any]:
    return {
        **_base_entry(config, selected),
        "software": software,
        "crds_context": crds_context,
        "input_path": str(input_path),
        "output_paths": [str(path) for path in outputs],
    }


def _download_record(result: DownloadResult) -> dict[str, Any]:
    return {
        "status": "reused" if result.reused else "downloaded",
        "path": str(result.path),
        "size_bytes": result.size_bytes,
    }


def _file_record(path: Path) -> dict[str, Any]:
    product_type = path.stem.rsplit("_", maxsplit=1)[-1]
    roles = {
        "rate": "stage1_exposure_average",
        "rateints": "stage2_input",
        "ramp": "stage1_calibrated_ramp_intermediate",
    }
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "product_type": product_type,
        "role": roles.get(product_type, "stage1_output"),
    }


def _stage2_file_record(path: Path) -> dict[str, Any]:
    product_type = path.stem.rsplit("_", maxsplit=1)[-1]
    if product_type == "calints":
        role = "future_tso3_association_input"
        future_tso3_input = True
    elif product_type == "x1dints":
        role = "stage2_per_exposure_extracted_spectrum"
        future_tso3_input = False
    else:
        role = "stage2_intermediate_or_auxiliary"
        future_tso3_input = False
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "product_type": product_type,
        "role": role,
        "future_tso3_association_input": future_tso3_input,
    }


def _pipeline_provenance(
    log_path: Path,
    pipeline_name: str,
    output_dir: Path,
    parameter_overrides: dict[str, Any],
) -> dict[str, Any]:
    summary = pipeline_log_summary(log_path, pipeline_name)
    configuration = summary["pipeline_configuration"]
    configuration["invocation"] = {
        "output_dir": str(output_dir),
        "save_results": True,
        "explicit_parameter_overrides": parameter_overrides,
    }
    return summary
