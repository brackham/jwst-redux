"""Auditable, workflow-level retention of regenerable pipeline products."""

from __future__ import annotations

import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import WriteConfig
from .provenance import ManifestStore, utc_now
from .workspace import Workspace


@dataclass(frozen=True)
class RetentionResult:
    """Outcome of applying one branch's configured retention policy."""

    policy: str
    endpoint: str
    status: str
    candidate_paths: tuple[Path, ...]
    deleted_paths: tuple[Path, ...]
    bytes_reclaimed: int
    reason: str | None
    manifest_entry: dict[str, Any]


def apply_retention(
    config: WriteConfig,
    endpoint: str,
    stage_entries: dict[str, tuple[dict[str, Any], ...]],
    *,
    qa_succeeded: bool,
) -> RetentionResult:
    """Apply retention after pipeline and QA provenance has been finalized.

    Only FITS files explicitly recorded by successful work in stages upstream of
    ``endpoint`` can become candidates. Every candidate is preflighted before the
    first unlink, and deletion progress is persisted after each file.
    """
    workspace = Workspace.existing_for_selection(
        config.discovery.output_root.resolve(), config.selection
    )
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    candidate_stages = {
        "stage1": (),
        "stage2": ("stage1",),
        "stage3": ("stage1", "stage2"),
    }[endpoint]
    candidate_files = _recorded_files(stage_entries, candidate_stages, fits_only=True)
    endpoint_files = _recorded_files(stage_entries, (endpoint,), fits_only=False)
    run_id = str(uuid.uuid4())
    entry = {
        "run_id": run_id,
        "operation": "retention",
        "selection": _selection_record(config),
        "retention_policy": config.retention,
        "endpoint": endpoint,
        "status": "running",
        "start_time": utc_now(),
        "end_time": None,
        "qa_enabled": config.qa_enabled,
        "qa_succeeded": qa_succeeded,
        "endpoint_output_paths": [item["path"] for item in endpoint_files],
        "candidate_paths": [item["path"] for item in candidate_files],
        "candidate_files": list(candidate_files),
        "candidate_bytes": sum(item["size_bytes"] for item in candidate_files),
        "deleted_paths": [],
        "bytes_reclaimed": 0,
    }
    manifest.append(entry)

    if config.retention == "all":
        return _finish(
            manifest,
            run_id,
            config,
            endpoint,
            candidate_files,
            reason="policy retains all products",
        )
    if not qa_succeeded:
        return _finish(
            manifest,
            run_id,
            config,
            endpoint,
            candidate_files,
            reason="enabled QA did not complete successfully",
        )
    if not candidate_files:
        return _finish(
            manifest,
            run_id,
            config,
            endpoint,
            candidate_files,
            reason="endpoint has no upstream pipeline products",
        )

    endpoint_error = _preflight(endpoint_files, workspace, allowed_stages=(endpoint,))
    if endpoint_error is not None:
        return _finish(
            manifest,
            run_id,
            config,
            endpoint,
            candidate_files,
            reason=f"endpoint output verification failed: {endpoint_error}",
        )
    candidate_error = _preflight(candidate_files, workspace, allowed_stages=candidate_stages)
    if candidate_error is not None:
        return _finish(
            manifest,
            run_id,
            config,
            endpoint,
            candidate_files,
            reason=f"candidate safety verification failed: {candidate_error}",
        )

    deleted: list[Path] = []
    reclaimed = 0
    for item in candidate_files:
        path = Path(item["path"])
        try:
            path.unlink()
        except OSError as error:
            reason = f"could not delete {path}: {error}"
            manifest.update(
                run_id,
                {
                    "status": "failed",
                    "end_time": utc_now(),
                    "deleted_paths": [str(value) for value in deleted],
                    "bytes_reclaimed": reclaimed,
                    "reason": reason,
                },
            )
            return _result(manifest, run_id, config, endpoint, candidate_files)
        deleted.append(path)
        reclaimed += item["size_bytes"]
        manifest.update(
            run_id,
            {
                "deleted_paths": [str(value) for value in deleted],
                "bytes_reclaimed": reclaimed,
            },
        )
    manifest.update(run_id, {"status": "success", "end_time": utc_now(), "reason": None})
    return _result(manifest, run_id, config, endpoint, candidate_files)


def _recorded_files(
    stage_entries: dict[str, tuple[dict[str, Any], ...]],
    stages: tuple[str, ...],
    *,
    fits_only: bool,
) -> tuple[dict[str, Any], ...]:
    """Return deterministic, de-duplicated output records from this workflow."""
    found: dict[str, dict[str, Any]] = {}
    for stage in stages:
        for entry in stage_entries.get(stage, ()):
            records = entry.get("outputs", [])
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                path = record.get("path")
                size = record.get("size_bytes")
                if not isinstance(path, str) or not isinstance(size, int):
                    continue
                if fits_only and not path.lower().endswith(".fits"):
                    continue
                found[path] = {"stage": stage, "path": path, "size_bytes": size}
    return tuple(found[path] for path in sorted(found))


def _preflight(
    files: tuple[dict[str, Any], ...],
    workspace: Workspace,
    *,
    allowed_stages: tuple[str, ...],
) -> str | None:
    if not files:
        return "no recorded outputs"
    if workspace.root.is_symlink():
        return f"workspace root is a symlink: {workspace.root}"
    for item in files:
        stage = item["stage"]
        path = Path(item["path"])
        expected_dir = getattr(workspace, stage)
        if stage not in allowed_stages:
            return f"unexpected stage {stage!r} for {path}"
        if not path.is_absolute() or path.parent != expected_dir:
            return f"recorded path is outside the expected {stage} directory: {path}"
        if expected_dir.is_symlink():
            return f"expected {stage} directory is a symlink: {expected_dir}"
        if path.is_symlink():
            return f"recorded output is a symlink: {path}"
        try:
            details = path.stat(follow_symlinks=False)
        except OSError as error:
            return f"cannot inspect recorded output {path}: {error}"
        if not stat.S_ISREG(details.st_mode):
            return f"recorded output is not a regular file: {path}"
        if item["size_bytes"] <= 0 or details.st_size != item["size_bytes"]:
            return f"recorded output is missing or changed: {path}"
    return None


def _finish(
    manifest: ManifestStore,
    run_id: str,
    config: WriteConfig,
    endpoint: str,
    candidate_files: tuple[dict[str, Any], ...],
    *,
    reason: str,
) -> RetentionResult:
    manifest.update(
        run_id,
        {"status": "skipped", "end_time": utc_now(), "reason": reason},
    )
    return _result(manifest, run_id, config, endpoint, candidate_files)


def _result(
    manifest: ManifestStore,
    run_id: str,
    config: WriteConfig,
    endpoint: str,
    candidate_files: tuple[dict[str, Any], ...],
) -> RetentionResult:
    entry = manifest.entry(run_id) or {}
    return RetentionResult(
        policy=config.retention,
        endpoint=endpoint,
        status=str(entry.get("status")),
        candidate_paths=tuple(Path(item["path"]) for item in candidate_files),
        deleted_paths=tuple(Path(path) for path in entry.get("deleted_paths", [])),
        bytes_reclaimed=int(entry.get("bytes_reclaimed", 0)),
        reason=entry.get("reason"),
        manifest_entry=entry,
    )


def _selection_record(config: WriteConfig) -> dict[str, str]:
    selection = config.selection
    record = {
        "program_id": selection.program_id,
        "observation_id": selection.observation_id,
        "visit_number": selection.visit_number,
        "exposure_number": selection.resolved_exposure_number,
        "exposure_id": selection.exposure_id,
        "label": selection.label,
        "workspace_name": selection.workspace_name,
    }
    if selection.detector is not None:
        record["detector"] = selection.detector
    return record
