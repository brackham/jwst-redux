"""Content identities with cheap metadata checks and bounded, stat-keyed hashing."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from .exceptions import JWSTReduxError


class _ChangedDuringRead(JWSTReduxError):
    pass


def metadata(path: Path) -> dict[str, Any]:
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        raise JWSTReduxError(f"Expected a nonempty regular file: {path}")
    return {
        "path": str(path),
        "size_bytes": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "ctime_ns": details.st_ctime_ns,
    }


def _signature(path: Path) -> tuple[int, ...]:
    details = path.stat()
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


@lru_cache(maxsize=2048)
def _digest(path: Path, signature: tuple[int, ...]) -> str:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if _signature(path) != signature:
        raise _ChangedDuringRead(f"File changed while computing its checksum: {path}")
    return digest


def checksum(path: Path) -> str:
    return _digest(path, _signature(path))


def _same_metadata(reference: dict[str, Any], current: dict[str, Any]) -> bool:
    return all(
        key in reference and reference[key] == current[key]
        for key in ("path", "size_bytes", "mtime_ns", "ctime_ns")
    )


def record_file(path: Path, known: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """Hash once at creation; reuse a digest only with unchanged metadata."""
    for _ in range(3):
        current = metadata(path)
        for reference in known:
            if reference.get("sha256") and _same_metadata(reference, current):
                return {**current, "sha256": reference["sha256"]}
        try:
            digest = checksum(path)
        except _ChangedDuringRead:
            continue
        if metadata(path) == current:
            return {**current, "sha256": digest}
    raise JWSTReduxError(f"File kept changing while recording its identity: {path}")


def file_matches(reference: dict[str, Any], known: Iterable[dict[str, Any]] = ()) -> bool:
    """Check size first, then metadata or a recorded content digest.

    Legacy size-only output records retain their original integrity check. A
    legacy timestamp that *differs* is never accepted without an earlier digest.
    New records always include timestamps and SHA-256, including adopted outputs.
    """
    try:
        path = Path(reference["path"])
        current = metadata(path)
        if reference.get("size_bytes") != current["size_bytes"]:
            return False
        digest = reference.get("sha256")
        if digest is None:
            for candidate in known:
                if (
                    candidate.get("path") == reference["path"]
                    and candidate.get("size_bytes") == reference["size_bytes"]
                    and candidate.get("sha256")
                    and (
                        "mtime_ns" not in reference
                        or candidate.get("mtime_ns") == reference["mtime_ns"]
                    )
                ):
                    digest = candidate["sha256"]
                    break
        if digest:
            if _same_metadata(reference, current):
                return True
            return record_file(path, known)["sha256"] == digest
        # Preserve the old size-only check; do not manufacture proof of past
        # contents by hashing the current file after a known timestamp change.
        return "mtime_ns" not in reference or (
            reference["mtime_ns"] == current["mtime_ns"]
            and ("ctime_ns" not in reference or reference["ctime_ns"] == current["ctime_ns"])
        )
    except (OSError, KeyError, TypeError, ValueError, JWSTReduxError):
        return False
