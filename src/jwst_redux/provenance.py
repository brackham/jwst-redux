"""Straightforward JSON provenance for the single-product Stage 1 milestone."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from . import __version__
from .exceptions import JWSTReduxError


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


def software_versions() -> dict[str, Any]:
    """Collect the software identity required to reproduce a run."""
    commit, dirty = _git_state()
    return {
        "jwst_redux_version": __version__,
        "jwst_redux_git_commit": commit,
        "jwst_redux_git_dirty": dirty,
        "python_version": platform.python_version(),
        "jwst_version": _distribution_version("jwst"),
        "astroquery_version": _distribution_version("astroquery"),
    }


def make_run_key(payload: dict[str, Any]) -> str:
    """Create a stable identity for resume matching."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ManifestStore:
    """Small atomic manifest store for resumable write operations."""

    def __init__(self, path: Path, scientific_target: str) -> None:
        self.path = path
        self.scientific_target = scientific_target

    def initialize(self) -> None:
        if self.path.exists():
            self._load()
            return
        self._write(
            {
                "schema_version": 1,
                "scientific_target": self.scientific_target,
                "runs": [],
            }
        )

    def append(self, entry: dict[str, Any]) -> str:
        data = self._load()
        run_id = str(entry.get("run_id") or uuid.uuid4())
        data["runs"].append({**entry, "run_id": run_id})
        self._write(data)
        return run_id

    def update(self, run_id: str, updates: dict[str, Any]) -> None:
        data = self._load()
        for entry in data["runs"]:
            if entry.get("run_id") == run_id:
                entry.update(updates)
                self._write(data)
                return
        raise JWSTReduxError(f"Manifest entry not found: {run_id}")

    def successful_run(self, run_key: str) -> dict[str, Any] | None:
        """Return the newest matching success whose recorded outputs remain intact."""
        data = self._load()
        for entry in reversed(data["runs"]):
            if entry.get("run_key") != run_key or entry.get("status") != "success":
                continue
            outputs = entry.get("outputs", [])
            if outputs and all(_recorded_file_is_intact(output) for output in outputs):
                return entry
        return None

    def successful_entry(
        self,
        operation: str,
        required_fields: dict[str, Any],
        software_requirements: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the newest intact success matching explicit provenance fields."""
        data = self._load()
        for entry in reversed(data["runs"]):
            if entry.get("operation") != operation or entry.get("status") != "success":
                continue
            if any(entry.get(key) != value for key, value in required_fields.items()):
                continue
            recorded_software = entry.get("software", {})
            if any(
                recorded_software.get(key) != value for key, value in software_requirements.items()
            ):
                continue
            outputs = entry.get("outputs", [])
            if outputs and all(_recorded_file_is_intact(output) for output in outputs):
                return entry
        return None

    def entry(self, run_id: str) -> dict[str, Any] | None:
        """Return a manifest entry by run identifier."""
        for entry in self._load()["runs"]:
            if entry.get("run_id") == run_id:
                return entry
        return None

    def entries(self) -> list[dict[str, Any]]:
        return list(self._load()["runs"])

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise JWSTReduxError(f"Cannot read manifest {self.path}: {error}") from error
        if data.get("schema_version") != 1 or not isinstance(data.get("runs"), list):
            raise JWSTReduxError(f"Unsupported manifest schema: {self.path}")
        if data.get("scientific_target") != self.scientific_target:
            raise JWSTReduxError(
                f"Manifest target {data.get('scientific_target')!r} does not match "
                f"{self.scientific_target!r}."
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(self.path)


def _recorded_file_is_intact(record: dict[str, Any]) -> bool:
    path = Path(str(record.get("path", "")))
    expected_size = record.get("size_bytes")
    return (
        path.is_file()
        and isinstance(expected_size, int)
        and expected_size > 0
        and path.stat().st_size == expected_size
    )


def _distribution_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def _git_state() -> tuple[str, bool | None]:
    repository = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown", None
    return commit, dirty
