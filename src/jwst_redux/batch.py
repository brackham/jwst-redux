"""Sequential execution of metadata-planned reduction branches.

The batch layer intentionally contains no target-specific pipeline rules.  It
uses the discovery hierarchy and the resolver's per-group official path, then
delegates actual work to the established isolated exposure workflow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BatchConfig, ExposureSelectionConfig, WriteConfig, write_config_for_exposure
from .mast.download import ProductDownloader
from .mast.query import DiscoveryResult, discover
from .models import DatasetPlan, Exposure, ReductionPlan, ScienceDataset
from .modes import observing_mode_for
from .pipeline.runner import PipelineCallable
from .planning.resolver import make_reduction_plans
from .provenance import ManifestStore
from .stage1 import ThroughWorkflowResult, run_selected_through
from .workspace import Workspace

Discoverer = Callable[[Any], DiscoveryResult]
ContextResolver = Callable[[str], str]


@dataclass(frozen=True)
class BatchBranch:
    """One exposure or association-group execution branch from a reduction plan."""

    dataset: ScienceDataset
    reduction: ReductionPlan
    exposure: Exposure
    config: WriteConfig
    endpoint: str
    segment_count: int
    stage_actions: tuple[tuple[str, str], ...]

    @property
    def label(self) -> str:
        if self.config.group_id:
            return f"{self.dataset.dataset_id} / {len(self.reduction.group.exposures)} exposures"
        return self.config.selection.label

    @property
    def pipeline_path(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.reduction.stages)


@dataclass(frozen=True)
class PreparedBatch:
    """Read-only discovery, planning, and resume assessment for a batch run."""

    discovery: DiscoveryResult
    plans: tuple[DatasetPlan, ...]
    branches: tuple[BatchBranch, ...]
    retention: str = "all"


@dataclass(frozen=True)
class BatchBranchResult:
    branch: BatchBranch
    status: str
    workflow: ThroughWorkflowResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class BatchWorkflowResult:
    prepared: PreparedBatch
    branches: tuple[BatchBranchResult, ...]

    @property
    def failures(self) -> tuple[BatchBranchResult, ...]:
        return tuple(result for result in self.branches if result.status == "failed")


def prepare_batch(config: BatchConfig, *, discoverer: Discoverer | None = None) -> PreparedBatch:
    """Discover and resolve every branch without writing, downloading, or CRDS access."""
    result = (discoverer or discover)(config.discovery)
    plans = make_reduction_plans(config.discovery, result.datasets)
    branches: list[BatchBranch] = []
    for dataset_plan in plans:
        for reduction in dataset_plan.reductions:
            endpoint = _endpoint(reduction)
            mode = observing_mode_for(reduction.group.exposures[0])
            members = (
                reduction.group.exposures[:1]
                if mode.execution_scope == "group"
                else reduction.group.exposures
            )
            for exposure in members:
                detector = dict(reduction.group.compatibility).get("detector")
                selection = _selection_for(dataset_plan.dataset, exposure, detector=detector)
                write_config = write_config_for_exposure(config, selection)
                if mode.execution_scope == "group":
                    from dataclasses import replace

                    from .group_workflow import group_workspace_id

                    write_config = replace(
                        write_config, group_id=group_workspace_id(reduction.group)
                    )
                stage_actions = tuple(
                    (stage.name, _stage_action(write_config, exposure, stage.name))
                    for stage in reduction.stages
                )
                branches.append(
                    BatchBranch(
                        dataset=dataset_plan.dataset,
                        reduction=reduction,
                        exposure=exposure,
                        config=write_config,
                        endpoint=endpoint,
                        segment_count=sum(
                            mode.execution_scope == "group"
                            or product.exposure_id == exposure.exposure_id
                            for product in reduction.group.products
                        ),
                        stage_actions=stage_actions,
                    )
                )
    return PreparedBatch(result, plans, tuple(branches), retention=config.retention)


def run_batch(
    config: BatchConfig,
    *,
    prepared: PreparedBatch | None = None,
    discoverer: Discoverer | None = None,
    downloader: ProductDownloader | None = None,
    detector1_pipeline: PipelineCallable | None = None,
    spec2_pipeline: PipelineCallable | None = None,
    tso3_pipeline: PipelineCallable | None = None,
    spec3_pipeline: PipelineCallable | None = None,
    context_resolver: ContextResolver | None = None,
) -> BatchWorkflowResult:
    """Execute prepared branches in order, retaining successful siblings on failure.

    With the default ``continue`` policy every branch is attempted.  ``stop``
    records the failed branch and leaves later branches untouched.  A later
    invocation uses the existing per-stage manifests to resume incomplete work.
    """
    execution = prepared or prepare_batch(config, discoverer=discoverer)
    results: list[BatchBranchResult] = []
    for branch in execution.branches:
        try:
            workflow = run_selected_through(
                branch.config,
                through=branch.endpoint,
                overwrite=config.overwrite,
                discoverer=lambda _: execution.discovery,
                downloader=downloader,
                detector1_pipeline=detector1_pipeline,
                spec2_pipeline=spec2_pipeline,
                tso3_pipeline=tso3_pipeline,
                spec3_pipeline=spec3_pipeline,
                context_resolver=context_resolver,
            )
        except Exception as error:  # noqa: BLE001 - branch failures must not erase siblings.
            results.append(BatchBranchResult(branch=branch, status="failed", error=str(error)))
            if config.failure_policy == "stop":
                break
        else:
            results.append(BatchBranchResult(branch=branch, status="success", workflow=workflow))
    return BatchWorkflowResult(execution, tuple(results))


def _selection_for(
    dataset: ScienceDataset, exposure: Exposure, *, detector: str | None = None
) -> ExposureSelectionConfig:
    identity = dict(dataset.identity)
    if exposure.exposure_id is None:
        raise ValueError("Planned exposure has no exposure identifier.")
    return ExposureSelectionConfig(
        program_id=identity["program_id"],
        observation_id=identity["observation_id"],
        visit_number=identity["visit_number"],
        exposure_id=exposure.exposure_id,
        detector=detector,
    )


def _endpoint(reduction: ReductionPlan) -> str:
    final_stage = reduction.stages[-1].name
    return {
        "Detector1Pipeline": "stage1",
        "Spec2Pipeline": "stage2",
        "Tso3Pipeline": "stage3",
        "Spec3Pipeline": "stage3",
    }[final_stage]


def _stage_action(config: WriteConfig, exposure: Exposure, pipeline_stage: str) -> str:
    """Report an intact local success as resume; exact identity is rechecked at run time."""
    workspace = Workspace.for_config(config)
    if not workspace.manifest.is_file():
        return "run"
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    operation = {
        "Detector1Pipeline": "stage1",
        "Spec2Pipeline": "stage2",
        "Tso3Pipeline": "stage3",
        "Spec3Pipeline": "stage3",
    }[pipeline_stage]
    for entry in reversed(manifest.entries()):
        if (
            entry.get("operation") == operation
            and entry.get("status") == "success"
            and (
                entry.get("exposure_identifier") == exposure.exposure_id
                or entry.get("group_id") == config.group_id
                and config.group_id is not None
            )
            and _outputs_intact(entry.get("outputs"))
        ):
            return "resume"
    return "run"


def _outputs_intact(records: object) -> bool:
    if not isinstance(records, list) or not records:
        return False
    for record in records:
        if not isinstance(record, dict):
            return False
        path = Path(str(record.get("path", "")))
        size = record.get("size_bytes")
        if (
            not path.is_file()
            or not isinstance(size, int)
            or size <= 0
            or path.stat().st_size != size
        ):
            return False
    return True
