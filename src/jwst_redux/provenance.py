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
from .file_identity import checksum, file_matches, record_file
from .security import redact


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
            if (
                outputs
                and all(file_matches(output, self._file_records(data)) for output in outputs)
                and (
                    "input_file" not in entry
                    or file_matches(entry["input_file"], self._file_records(data))
                )
            ):
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
            if any(
                entry.get(key) != value
                for key, value in required_fields.items()
                if key not in {"association", "input_file"}
            ):
                continue
            recorded_software = entry.get("software", {})
            if any(
                recorded_software.get(key) != value for key, value in software_requirements.items()
            ):
                continue
            known = self._file_records(data)
            if "association" in required_fields:
                old = entry.get("association", {})
                current = required_fields["association"]
                if {k: v for k, v in old.items() if k != "member_files"} != {
                    k: v for k, v in current.items() if k != "member_files"
                }:
                    from .associations import equivalent_association_records

                    if not equivalent_association_records(old, current):
                        continue
                old_files, current_files = (
                    old.get("member_files", []),
                    current.get("member_files", []),
                )
                if len(old_files) != len(current_files) or any(
                    before.get("path") != after.get("path")
                    or before.get("size_bytes") != after.get("size_bytes")
                    or not file_matches(before, known)
                    for before, after in zip(old_files, current_files, strict=True)
                ):
                    continue
            if "input_file" in required_fields:
                old = entry.get("input_file") or {
                    "path": entry.get("input_path"),
                    "size_bytes": entry.get("input_size_bytes")
                    or entry.get("download", {}).get("size_bytes"),
                }
                if old.get("path") != required_fields["input_file"]["path"] or not file_matches(
                    old, known
                ):
                    continue
            outputs = entry.get("outputs", [])
            if outputs and all(file_matches(output, known) for output in outputs):
                return entry
        return None

    @staticmethod
    def _file_records(data: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        for entry in reversed(data["runs"]):
            # A running invocation is an observation of today's contents, not
            # evidence that a previous invocation consumed those contents.
            if entry.get("status") not in {"success", "skipped"}:
                continue
            records.extend(entry.get("outputs", []))
            if entry.get("input_file"):
                records.append(entry["input_file"])
            records.extend(entry.get("association", {}).get("member_files", []))
            if entry.get("operation") == "checksum_baseline":
                for item in entry["files"]:
                    records.extend((item["reference"], item["observed"]))
        return records

    def file_record(self, path: Path) -> dict[str, Any]:
        """Capture current content, reusing trustworthy unchanged metadata."""
        return record_file(path, self._file_records(self._load()))

    def file_is_intact(self, reference: dict[str, Any]) -> bool:
        return file_matches(reference, self._file_records(self._load()))

    def refreshed_outputs(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        """Record verified reuse without rewriting historical success entries."""
        return [{**item, **self.file_record(Path(item["path"]))} for item in entry["outputs"]]

    def import_checksums(self, baseline: Path) -> int:
        """Verify an independently recorded baseline and append an audit receipt.

        BASELINE is {"files": [{"path", "size_bytes", "mtime_ns", "sha256"}, ...]}.
        Callers must supply a trusted snapshot of the earlier files. Computing a
        new checksum after an unexplained change cannot establish past identity.
        Only paths already referenced by this manifest are imported, atomically.
        """
        data = self._load()
        paths = {item["path"] for item in self._file_records(data)}
        paths.update(e["input_path"] for e in data["runs"] if e.get("input_path"))
        source = json.loads(baseline.read_text())
        verified = []
        for reference in source["files"]:
            if reference["path"] not in paths:
                continue
            digest = reference.get("sha256", "")
            if (
                len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or not isinstance(reference.get("mtime_ns"), int)
            ):
                raise JWSTReduxError("Checksum baseline requires SHA-256 and recorded timestamps.")
            observed = record_file(Path(reference["path"]))
            if observed["sha256"] != digest or observed["size_bytes"] != reference["size_bytes"]:
                raise JWSTReduxError(f"Checksum baseline mismatch: {reference['path']}")
            verified.append({"reference": reference, "observed": observed})
        if verified:
            self.append(
                {
                    "operation": "checksum_baseline",
                    "status": "success",
                    "end_time": utc_now(),
                    "source": {"path": str(baseline), "sha256": checksum(baseline)},
                    "files": verified,
                }
            )
        return len(verified)

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
            json.dump(redact(data), handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(self.path)


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
