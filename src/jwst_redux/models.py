"""Core domain models shared across archive discovery, planning, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Observation:
    program_id: str | None
    observation_id: str | None
    visit_id: str | None
    target_name: str | None
    instrument: str | None
    exposure_type: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Product:
    uri: str
    filename: str
    calibration_level: str | None = None
    product_type: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineStage:
    name: str
    inputs: tuple[Path, ...]
    output_dir: Path
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReductionPlan:
    observation: Observation
    stages: tuple[PipelineStage, ...]
