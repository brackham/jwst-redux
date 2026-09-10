"""Resolve normalized metadata into the appropriate official JWST pipeline path."""

from __future__ import annotations

from pathlib import Path

from ..config import DiscoveryConfig
from ..exceptions import PlanningError
from ..models import (
    DatasetPlan,
    PipelineStage,
    ReductionGroup,
    ReductionPlan,
    ScienceDataset,
)
from ..pipeline.stages import pipeline_path_for
from .associations import build_compatible_groups


def make_reduction_plans(
    config: DiscoveryConfig,
    datasets: tuple[ScienceDataset, ...],
) -> tuple[DatasetPlan, ...]:
    """Create dataset-level plans containing compatible exposure branches."""
    plans = []
    for dataset in datasets:
        groups = build_compatible_groups(dataset)
        plans.append(
            DatasetPlan(
                dataset=dataset,
                reductions=tuple(_plan_group(config, group) for group in groups),
            )
        )
    return tuple(plans)


def _plan_group(config: DiscoveryConfig, group: ReductionGroup) -> ReductionPlan:
    paths = tuple(pipeline_path_for(exposure) for exposure in group.exposures)
    classes = paths[0].classes
    if any(path.classes != classes for path in paths[1:]):
        raise PlanningError(f"Group {group.group_id} contains incompatible pipeline paths.")

    raw_inputs = tuple(config.output_root / "raw" / item.filename for item in group.products)
    rateints_inputs = tuple(
        config.output_root / "stage1" / _replace_suffix(item.filename, "_uncal", "_rateints")
        for item in group.products
    )
    calints_inputs = tuple(
        config.output_root / "stage2" / _replace_suffix(item.filename, "_uncal", "_calints")
        for item in group.products
    )
    stages = [
        PipelineStage(
            name="Detector1Pipeline",
            inputs=raw_inputs,
            output_dir=config.output_root / "stage1",
            input_suffix="_uncal",
            output_suffixes=("_rate", "_rateints"),
        ),
    ]
    if "Spec2Pipeline" in classes:
        stages.append(
            PipelineStage(
                name="Spec2Pipeline",
                inputs=rateints_inputs,
                output_dir=config.output_root / "stage2",
                input_suffix="_rateints",
                output_suffixes=("_calints", "_x1dints"),
            )
        )
    if "Tso3Pipeline" in classes:
        stages.append(
            PipelineStage(
                name="Tso3Pipeline",
                inputs=calints_inputs,
                output_dir=config.output_root / "stage3",
                input_suffix="_calints",
                output_suffixes=("_crfints", "_x1dints", "_whtlt"),
                association_required=True,
            )
        )
    notes = tuple(dict.fromkeys(path.note for path in paths if path.note is not None))
    return ReductionPlan(
        group=group,
        archive_products=group.products,
        stages=tuple(stages),
        notes=notes,
    )


def _replace_suffix(filename: str, old: str, new: str) -> Path:
    expected = f"{old}.fits"
    if not filename.endswith(expected):
        raise PlanningError(f"Cannot derive {new} name from {filename}.")
    return Path(f"{filename[: -len(expected)]}{new}.fits")
