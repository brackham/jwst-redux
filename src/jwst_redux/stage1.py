"""Single-product download and official Detector1 orchestration."""

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
    resolve_crds_context,
    run_detector1,
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


def _discover_selected(
    config: WriteConfig, discoverer: Discoverer | None
) -> SelectedProduct:
    result = (discoverer or discover)(config.discovery)
    return select_stage1_product(result.datasets, config.selection)


def _base_entry(config: WriteConfig, selected: SelectedProduct) -> dict[str, Any]:
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
        "pipeline_class": "jwst.pipeline.Detector1Pipeline",
        "explicit_parameter_overrides": config.parameter_overrides,
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
    return {"path": str(path), "size_bytes": path.stat().st_size}
