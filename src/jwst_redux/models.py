"""Core domain models shared across archive discovery, planning, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Exposure:
    """One exposure-level record returned by the MAST JWST mission service."""

    program_id: str | None
    observation_id: str | None
    visit_id: str | None
    target_name: str | None
    instrument: str | None
    exposure_type: str | None
    archive_id: str | None = None
    exposure_id: str | None = None
    visit_number: str | None = None
    is_tso: bool | None = None
    optical_elements: str | None = None
    subarray: str | None = None
    start_time: str | None = None
    duration_seconds: float | None = None
    integration_count: int | None = None
    group_count: int | None = None
    segment_count: int | None = None
    access: str | None = None
    products: tuple[Product, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Product:
    uri: str
    filename: str
    calibration_level: str | None = None
    product_type: str | None = None
    size_bytes: int | None = None
    exposure_id: str | None = None
    suffix: str | None = None
    access: str | None = None
    segment_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScienceDataset:
    """A scientifically meaningful dataset containing one or more exposures."""

    dataset_id: str
    identity: tuple[tuple[str, str], ...]
    exposures: tuple[Exposure, ...]


@dataclass(frozen=True)
class SelectedProduct:
    """One product selected through its scientific dataset and exposure parents."""

    dataset: ScienceDataset
    exposure: Exposure
    product: Product


@dataclass(frozen=True)
class SelectedExposure:
    """One explicitly selected exposure and all of its starting products."""

    dataset: ScienceDataset
    exposure: Exposure
    products: tuple[Product, ...]


@dataclass(frozen=True)
class ReductionGroup:
    """Exposures sharing one compatible reduction/association path."""

    group_id: str
    dataset_id: str
    compatibility: tuple[tuple[str, str], ...]
    exposures: tuple[Exposure, ...]
    products: tuple[Product, ...]


@dataclass(frozen=True)
class PipelineStage:
    name: str
    inputs: tuple[Path, ...]
    output_dir: Path
    input_suffix: str | None = None
    output_suffixes: tuple[str, ...] = ()
    association_required: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReductionPlan:
    group: ReductionGroup
    archive_products: tuple[Product, ...]
    stages: tuple[PipelineStage, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatasetPlan:
    """All exposure-specific reduction branches for one scientific dataset."""

    dataset: ScienceDataset
    reductions: tuple[ReductionPlan, ...]
