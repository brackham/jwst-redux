"""Filesystem layout and workspace helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import ExposureSelectionConfig


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def for_selection(cls, output_root: Path, selection: ExposureSelectionConfig) -> Workspace:
        """Return the isolated workspace for one explicit selected exposure."""
        return cls(output_root / selection.workspace_name)

    @classmethod
    def existing_for_selection(
        cls, output_root: Path, selection: ExposureSelectionConfig
    ) -> Workspace:
        """Locate an existing isolated workspace or a matching pre-isolation workspace.

        The fallback is limited to already-created local workspaces whose manifest
        identifies the same complete selection. It preserves resume and QA access
        for validated pre-isolation reductions without mixing visits.
        """
        isolated = cls.for_selection(output_root, selection)
        if isolated.manifest.is_file() or isolated.root.exists():
            return isolated
        legacy = cls(output_root)
        if legacy.manifest.is_file() and _manifest_matches_selection(legacy.manifest, selection):
            return legacy
        return isolated

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def stage1(self) -> Path:
        return self.root / "stage1"

    @property
    def stage2(self) -> Path:
        return self.root / "stage2"

    @property
    def stage3(self) -> Path:
        return self.root / "stage3"

    @property
    def associations(self) -> Path:
        """Official JWST association files generated for local pipeline runs."""
        return self.stage3 / "associations"

    def qa(self, stage: str) -> Path:
        """Return the derived QA directory for one calibrated pipeline stage."""
        if stage not in {"stage1", "stage2", "stage3"}:
            raise ValueError(f"Unsupported QA stage: {stage}")
        return getattr(self, stage) / "qa"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    def create(self) -> None:
        for path in (
            self.root,
            self.raw,
            self.stage1,
            self.stage2,
            self.stage3,
            self.associations,
            self.qa("stage1"),
            self.qa("stage2"),
            self.qa("stage3"),
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _manifest_matches_selection(path: Path, selection: ExposureSelectionConfig) -> bool:
    """Return whether a legacy manifest contains the complete selected exposure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entries = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return False
    identity = {
        "program_id": selection.program_id,
        "observation_id": selection.observation_id,
        "visit_number": selection.visit_number,
    }
    return any(
        isinstance(entry, dict)
        and entry.get("exposure_identifier") == selection.exposure_id
        and entry.get("scientific_dataset") == identity
        for entry in entries
    )
