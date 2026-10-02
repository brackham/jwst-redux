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
from ..modes import observing_mode_for
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
    mode = observing_mode_for(group.exposures[0])
    if any(path.classes != classes for path in paths[1:]):
        raise PlanningError(f"Group {group.group_id} contains incompatible pipeline paths.")

    raw_inputs = tuple(config.output_root / "raw" / item.filename for item in group.products)
    rateints_inputs = tuple(
        config.output_root
        / "stage1"
        / _replace_suffix(item.filename, "_uncal", mode.stage2_input_suffix)
        for item in group.products
    )
    calints_inputs = tuple(
        config.output_root
        / "stage2"
        / _replace_suffix(item.filename, "_uncal", mode.stage3_input_suffix)
        for item in group.products
    )
    stages = [
        PipelineStage(
            name="Detector1Pipeline",
            inputs=raw_inputs,
            output_dir=config.output_root / "stage1",
            input_suffix="_uncal",
            output_suffixes=("_rate", "_rateints") if mode.is_tso else ("_rate",),
        ),
    ]
    if "Spec2Pipeline" in classes:
        stages.append(
            PipelineStage(
                name="Spec2Pipeline",
                inputs=rateints_inputs,
                output_dir=config.output_root / "stage2",
                input_suffix=mode.stage2_input_suffix,
                output_suffixes=mode.stage2_output_suffixes,
                association_required=mode.stage2_association_rule is not None,
                association_scope="per_science" if mode.stage2_association_rule else None,
            )
        )
    if mode.stage3_class in classes:
        stages.append(
            PipelineStage(
                name=mode.stage3_class,
                inputs=calints_inputs,
                output_dir=config.output_root / "stage3",
                input_suffix=mode.stage3_input_suffix,
                output_suffixes=mode.stage3_output_suffixes,
                association_required=True,
            )
        )
    notes = tuple(dict.fromkeys(path.note for path in paths if path.note is not None))
    if mode.stage2_association_rule:
        notes += (
            (
                "Stage 2 resolves archived official nod associations, one science exposure per association. "
                "Execution requires every archived member; missing or ambiguous assignments fail before science downloads."
            ),
        )
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
