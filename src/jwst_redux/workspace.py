"""Filesystem layout and workspace helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    root: Path

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
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)
