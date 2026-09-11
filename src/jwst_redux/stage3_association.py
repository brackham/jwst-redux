"""Construction of local, official JWST Level-3 associations."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import PipelineExecutionError


@dataclass(frozen=True)
class Stage3Association:
    """A serialized official association and its stable local identity."""

    path: Path
    content_sha256: str
    member_paths: tuple[Path, ...]
    product_name: str
    reused: bool


def create_tso3_association(
    member_paths: tuple[Path, ...], association_dir: Path, product_name: str
) -> Stage3Association:
    """Serialize a valid JWST Level-3 association for local TSO3 execution.

    ``jwst.associations.asn_from_list`` is STScI's supported user-association
    API.  Its serializer validates the official association schema before any
    file is written.  Members use paths relative to the association directory,
    so the local association continues to resolve after the workspace moves.
    """
    if not member_paths:
        raise PipelineExecutionError("Cannot create a TSO3 association with no _calints members.")
    resolved_members = tuple(path.resolve() for path in member_paths)
    if len(resolved_members) != len(set(resolved_members)):
        raise PipelineExecutionError("TSO3 association members must be unique.")
    if len({path.name for path in resolved_members}) != len(resolved_members):
        raise PipelineExecutionError("TSO3 association member filenames must be unique.")
    missing = [path for path in resolved_members if not path.is_file()]
    if missing:
        raise PipelineExecutionError(
            "Cannot create a TSO3 association; member(s) are missing: "
            + ", ".join(str(path) for path in missing)
        )

    from jwst.associations.asn_from_list import asn_from_list

    member_expnames = [
        os.path.relpath(member, start=association_dir) for member in resolved_members
    ]
    association = asn_from_list(member_expnames, product_name=product_name)
    _suggested_name, serialized = association.dump(format="json")
    content_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    path = association_dir / f"{product_name}-{content_sha256[:12]}_asn.json"
    association_dir.mkdir(parents=True, exist_ok=True)
    reused = path.is_file() and path.read_text(encoding="utf-8") == serialized
    if not reused:
        path.write_text(serialized, encoding="utf-8")
    return Stage3Association(
        path=path,
        content_sha256=content_sha256,
        member_paths=resolved_members,
        product_name=product_name,
        reused=reused,
    )
