"""Filesystem layout and workspace helpers."""

from __future__ import annotations

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
