"""Command-line interface for jwst-redux."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .batch import BatchWorkflowResult, PreparedBatch, prepare_batch, run_batch
from .config import (
    DiscoveryConfig,
    load_batch_config,
    load_config,
    load_discovery_config,
    load_write_config,
)
from .exceptions import JWSTReduxError
from .mast.query import DiscoveryResult, discover
from .models import DatasetPlan, Product, ReductionPlan, ScienceDataset
from .planning.resolver import make_reduction_plans
from .qa.workflow import generate_qa
from .stage1 import (
    SegmentWorkflowResult,
    Stage1WorkflowResult,
    Stage2WorkflowResult,
    Stage3WorkflowResult,
    download_selected,
    run_selected_through,
)

app = typer.Typer(no_args_is_help=True, help="Retrieve and reduce JWST datasets reproducibly.")
console = Console()


class ThroughStage(str, Enum):
    stage1 = "stage1"
    stage2 = "stage2"
    stage3 = "stage3"


class QAStage(str, Enum):
    stage1 = "stage1"
    stage2 = "stage2"
    stage3 = "stage3"


def _config(path: Path) -> dict:
    return load_config(path)


@app.command()
def search(config: Path) -> None:
    """Search MAST and report scientific datasets, exposures, and products."""
    try:
        discovery_config = load_discovery_config(config)
        result = discover(discovery_config)
    except (JWSTReduxError, OSError, TypeError) as error:
        _abort(error)
    _print_search(discovery_config, result)


@app.command()
def plan(config: Path) -> None:
    """Show what would be downloaded and which pipeline stages would run."""
    try:
        discovery_config = load_discovery_config(config)
        result = discover(discovery_config)
        plans = make_reduction_plans(
            discovery_config,
            result.datasets,
        )
    except (JWSTReduxError, OSError, TypeError) as error:
        _abort(error)
    _print_plan(discovery_config, result, plans)


@app.command()
def download(
    config: Path,
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing raw file."),
) -> None:
    """Download the single Stage 1 product selected in CONFIG."""
    try:
        write_config = load_write_config(config)
        result = download_selected(write_config, overwrite=overwrite)
    except (JWSTReduxError, OSError, TypeError) as error:
        _abort(error)
    action = "Reused" if result.download.reused else "Downloaded"
    console.print(f"Selected: {write_config.selection.label}")
    console.print(
        f"[green]{action}[/] {result.download.path} ({_format_bytes(result.download.size_bytes)})"
    )


@app.command()
def run(
    config: Path,
    through: Annotated[
        ThroughStage | None,
        typer.Option("--through", help="Run through this pipeline stage."),
    ] = None,
    all_branches: bool = typer.Option(
        False,
        "--all",
        help="Sequentially run every planner-selected branch to its planned endpoint.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Redownload the raw input and rerun all requested stages.",
    ),
) -> None:
    """Run an explicit selected exposure, or all planned branches with ``--all``."""
    if all_branches:
        if through is not None:
            _abort(
                JWSTReduxError(
                    "--through is not used with --all; every branch uses its planned endpoint."
                )
            )
        try:
            batch_config = load_batch_config(config)
            prepared = prepare_batch(batch_config)
        except (JWSTReduxError, OSError, TypeError, ValueError) as error:
            _abort(error)
        _print_batch_summary(prepared)
        result = run_batch(batch_config, prepared=prepared)
        _print_batch_result(result)
        if result.failures:
            raise typer.Exit(code=1)
        return
    try:
        write_config = load_write_config(config)
        result = run_selected_through(
            write_config,
            through=(through or ThroughStage.stage1).value,
            overwrite=overwrite,
        )
    except (JWSTReduxError, OSError, TypeError) as error:
        _abort(error)
    console.print(f"Selected: {write_config.selection.label}")
    for segment in result.segments:
        _print_segment_result(segment)
    if result.stage3 is not None:
        _print_stage3_result(result.stage3)
    elif result.stage3_readiness is not None:
        console.print(
            "[green]Stage 3 ready:[/] "
            f"{len(result.stage3_readiness.calints_inputs)} intact _calints inputs recorded; "
            "run with --through stage3 to create the association and execute Tso3Pipeline."
        )
    console.print(f"Manifest: {result.segments[0].stage1.log_path.parent.parent / 'manifest.json'}")


@app.command()
def qa(
    config: Path,
    stage: Annotated[
        list[QAStage] | None,
        typer.Option("--stage", help="Stage(s) to regenerate; defaults to all successful stages."),
    ] = None,
    force: bool = typer.Option(False, "--force", help="Regenerate even current QA products."),
) -> None:
    """Regenerate QA from successful local pipeline products only."""
    try:
        write_config = load_write_config(config)
        results = generate_qa(
            write_config,
            stages=tuple(item.value for item in stage or ()) or ("stage1", "stage2", "stage3"),
            force=force,
        )
    except (JWSTReduxError, OSError, TypeError) as error:
        _abort(error)
    console.print(f"Selected: {write_config.selection.label}")
    for result in results:
        color = "green" if result.status in {"success", "skipped"} else "red"
        console.print(f"[{color}]QA {result.stage} {result.status}[/]: {result.input_paths[0]}")
        for output in result.outputs:
            console.print(f"  {output}")


def _print_segment_result(result: SegmentWorkflowResult) -> None:
    console.print(f"[bold]Segment {result.stage1.selected.product.segment_number:03d}[/]")
    _print_stage1_result(result.stage1)
    if result.stage2 is not None:
        _print_stage2_result(result.stage2)


def _print_stage1_result(result: Stage1WorkflowResult) -> None:
    download_action = "reused" if result.download.reused else "downloaded"
    console.print(
        f"Raw input {download_action}: {result.download.path} "
        f"({_format_bytes(result.download.size_bytes)})"
    )
    if result.status == "skipped":
        console.print(
            "[green]Detector1Pipeline skipped:[/] matching successful manifest run found."
        )
    else:
        console.print(f"[green]Detector1Pipeline complete[/] in {result.elapsed_seconds:.1f} s")
    for output in result.outputs:
        console.print(f"  {output} ({_format_bytes(output.stat().st_size)})")
    console.print(f"CRDS context: {result.crds_context}")
    console.print(f"Pipeline log: {result.log_path}")


def _print_stage2_result(result: Stage2WorkflowResult) -> None:
    console.print(f"Stage 2 input from manifest: {result.input_path}")
    if result.status == "skipped":
        console.print("[green]Spec2Pipeline skipped:[/] matching successful manifest run found.")
    else:
        console.print(f"[green]Spec2Pipeline complete[/] in {result.elapsed_seconds:.1f} s")
    for output in result.outputs:
        role = (
            "future TSO3 association input"
            if output.name.endswith("_calints.fits")
            else "per-exposure extracted spectrum; not a TSO3 association input"
        )
        console.print(f"  {output} ({_format_bytes(output.stat().st_size)}); {role}")
    console.print(f"CRDS context: {result.crds_context}")
    console.print(f"Pipeline log: {result.log_path}")


def _print_stage3_result(result: Stage3WorkflowResult) -> None:
    console.print(f"Stage 3 association: {result.association.path}")
    console.print(f"Association SHA-256: {result.association.content_sha256}")
    if result.status == "skipped":
        console.print("[green]Tso3Pipeline skipped:[/] matching successful manifest run found.")
    else:
        console.print(f"[green]Tso3Pipeline complete[/] in {result.elapsed_seconds:.1f} s")
    for output in result.outputs:
        console.print(f"  {output} ({_format_bytes(output.stat().st_size)})")
    console.print(f"CRDS context: {result.crds_context}")
    console.print(f"Pipeline log: {result.log_path}")


@app.command()
def status(config: Path) -> None:
    """Report reduction/download status from the manifest."""
    _config(config)
    console.print("[yellow]Not implemented yet:[/] manifest status")


@app.command(name="all")
def run_all(config: Path) -> None:
    """Search, plan, download, and run the complete workflow."""
    _config(config)
    console.print("[yellow]Not implemented yet:[/] complete workflow")


def _print_search(config: DiscoveryConfig, result: DiscoveryResult) -> None:
    query = config.query
    console.print(f"[bold]MAST JWST search: {query.target}[/]")
    console.print(
        f"Strategy: {query.target_match}; instrument={query.instrument}; "
        f"EXP_TYPE={query.exposure_type}; productLevel=1b"
    )
    console.print("Archive target names: " + ", ".join(query.archive_target_names))
    programs = "all" if query.proposal_ids is None else ", ".join(query.proposal_ids)
    console.print(f"Proposal/program IDs: {programs}")
    for number, dataset in enumerate(result.datasets, start=1):
        console.print(f"\n[bold cyan]Dataset {number}: {_dataset_label(dataset)}[/]")
        console.print("Identity: " + _format_identity(dataset.identity))
        for exposure_number, exposure in enumerate(dataset.exposures, start=1):
            console.print(f"  [bold]Exposure {exposure_number}: {_show(exposure.exposure_id)}[/]")
            console.print(
                f"    Archive target: {_show(exposure.target_name)}; "
                f"start={_format_time(exposure.start_time)} UTC; "
                f"TSO={_show(exposure.is_tso)}; access={_show(exposure.access)}"
            )
            console.print(
                f"    Optical elements: {_show(exposure.optical_elements)}; "
                f"subarray={_show(exposure.subarray)}; "
                f"NINTS={_show(exposure.integration_count)}; "
                f"NGROUPS={_show(exposure.group_count)}"
            )
            exposure_products = exposure.products
            console.print(f"    _uncal products ({len(exposure_products)}):")
            for product in exposure_products:
                console.print(
                    f"      Segment {_show(product.segment_number)}: {product.filename} "
                    f"({_format_bytes(product.size_bytes)})"
                )
    console.print(_search_summary(result))
    console.print("[dim]Read-only query: no files were downloaded or created.[/]")


def _print_plan(
    config: DiscoveryConfig,
    result: DiscoveryResult,
    plans: tuple[DatasetPlan, ...],
) -> None:
    console.print(f"[bold]Reduction plan: {config.query.target}[/]")
    reduction_count = sum(len(plan.reductions) for plan in plans)
    console.print(
        f"{_counted(len(plans), _dataset_noun(result.datasets))} containing "
        f"{_counted(len(result.exposures), 'exposure')}; "
        + _counted(
            reduction_count,
            "compatible reduction branch",
            "compatible reduction branches",
        )
    )
    for dataset_number, dataset_plan in enumerate(plans, start=1):
        dataset = dataset_plan.dataset
        console.print(f"\n[bold cyan]Dataset {dataset_number}: {_dataset_label(dataset)}[/]")
        console.print("Identity: " + _format_identity(dataset.identity))
        console.print(
            f"{_counted(len(dataset.exposures), 'exposure')}; "
            + _counted(
                len(dataset_plan.reductions),
                "reduction branch",
                "reduction branches",
            )
        )
        for branch_number, reduction_plan in enumerate(dataset_plan.reductions, start=1):
            _print_reduction_branch(branch_number, reduction_plan)

    planned_products = tuple(
        product
        for dataset_plan in plans
        for reduction in dataset_plan.reductions
        for product in reduction.archive_products
    )
    console.print(_product_summary(planned_products, noun="_uncal archive input"))
    console.print(
        "[dim]Plan only: no downloads, CRDS access, workspace creation, association writing, "
        "or pipeline execution occurred.[/]"
    )


def _print_reduction_branch(number: int, reduction_plan: ReductionPlan) -> None:
    group = reduction_plan.group
    console.print(f"\n  [bold]Branch {number}: {group.group_id}[/]")
    console.print("  Compatibility: " + _format_compatibility(group.compatibility))
    console.print(
        "  Exposure members: "
        + ", ".join(exposure.exposure_id or "unknown" for exposure in group.exposures)
    )

    console.print("  Archive inputs:")
    for product in reduction_plan.archive_products:
        console.print(
            f"    Segment {_show(product.segment_number)}: {product.filename}\n"
            f"      {_format_bytes(product.size_bytes)}; access={_show(product.access)}"
        )

    stage_table = Table(show_header=True, header_style="bold")
    stage_table.add_column("Stage")
    stage_table.add_column("Official class")
    stage_table.add_column("Planned flow")
    for stage_number, stage in enumerate(reduction_plan.stages, start=1):
        association = " as one association" if stage.association_required else " independently"
        outputs = ", ".join(stage.output_suffixes)
        stage_table.add_row(
            str(stage_number),
            stage.name,
            f"{len(stage.inputs)} {stage.input_suffix} input(s){association} -> {outputs}",
        )
    console.print(stage_table)
    for note in reduction_plan.notes:
        console.print(f"  [yellow]Pipeline limitation:[/] {note}")


def _print_batch_summary(prepared: PreparedBatch) -> None:
    """Show all branch decisions before the first download or pipeline invocation."""
    console.print("[bold]Batch execution plan: planned endpoints[/]")
    console.print(
        f"{_counted(len(prepared.plans), _dataset_noun(prepared.discovery.datasets))} containing "
        f"{_counted(len(prepared.discovery.exposures), 'exposure')} and "
        f"{_counted(len(prepared.branches), 'execution branch')}."
    )
    table = Table(show_header=True, header_style="bold")
    table.add_column("Dataset / exposure")
    table.add_column("Segments", justify="right")
    table.add_column("Planned pipeline path")
    table.add_column("Action")
    for branch in prepared.branches:
        stages = " → ".join(stage.removesuffix("Pipeline") for stage in branch.pipeline_path)
        actions = ", ".join(
            f"{stage.removesuffix('Pipeline')}={action}" for stage, action in branch.stage_actions
        )
        table.add_row(branch.label, str(branch.segment_count), stages, actions)
    console.print(table)
    console.print(
        "[dim]No download or calibration starts until this summary has been printed. "
        "Batch policy is sequential; the configured failure policy controls later branches.[/]"
    )


def _print_batch_result(result: BatchWorkflowResult) -> None:
    for branch_result in result.branches:
        if branch_result.status == "success":
            console.print(
                f"[green]Completed[/] {branch_result.branch.label} "
                f"through {branch_result.branch.endpoint}."
            )
        else:
            console.print(f"[red]Failed[/] {branch_result.branch.label}: {branch_result.error}")
    if result.failures:
        console.print(
            f"[yellow]{len(result.failures)} branch failure(s) recorded; successful siblings "
            "are preserved and a rerun resumes incomplete work.[/]"
        )


def _search_summary(result: DiscoveryResult) -> str:
    sizes = _size_summary(result.products)
    return (
        f"Total: {_counted(len(result.datasets), _dataset_noun(result.datasets))} containing "
        f"{_counted(len(result.exposures), 'exposure')} and "
        f"{_counted(len(result.products), '_uncal product')}, {sizes}"
    )


def _product_summary(products: tuple[Product, ...], *, noun: str) -> str:
    return f"Total: {_counted(len(products), noun)}, {_size_summary(products)}"


def _size_summary(products: tuple[Product, ...]) -> str:
    known = [product.size_bytes for product in products if product.size_bytes is not None]
    unknown = len(products) - len(known)
    summary = _format_bytes(sum(known))
    if unknown:
        summary += f" plus {unknown} product(s) with unknown size"
    return summary


def _format_bytes(size: int | None) -> str:
    if size is None:
        return "unknown"
    if size < 1_000_000_000:
        return f"{size / 1_000_000:.2f} MB ({size / 2**20:.2f} MiB)"
    return f"{size / 1_000_000_000:.2f} GB ({size / 2**30:.2f} GiB)"


def _format_time(value: str | None) -> str:
    if value is None:
        return "—"
    return value.replace("T", " ").split(".", maxsplit=1)[0]


def _format_compatibility(values: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{key}={value}" for key, value in values)


def _format_identity(values: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{key}={value}" for key, value in values)


def _dataset_label(dataset: ScienceDataset) -> str:
    identity = dict(dataset.identity)
    program = identity.get("program_id", "unknown")
    observation = identity.get("observation_id", "unknown")
    visit = identity.get("visit_number", "unknown")
    program_label = str(int(program)) if program.isdigit() else program
    proposal_type = str(dataset.exposures[0].metadata.get("proposal_type") or "Program").upper()
    return f"{proposal_type}-{program_label} Obs {observation} / Visit {visit}"


def _dataset_noun(datasets: tuple[ScienceDataset, ...]) -> str:
    if not datasets:
        return "science dataset"
    exposure = datasets[0].exposures[0]
    labels = {
        ("NIRISS", "NIS_SOSS"): "SOSS dataset",
        ("NIRSPEC", "NRS_BRIGHTOBJ"): "BOTS dataset",
    }
    return labels.get((exposure.instrument, exposure.exposure_type), "science dataset")


def _counted(count: int, singular: str, plural: str | None = None) -> str:
    noun = singular if count == 1 else plural or f"{singular}s"
    return f"{count} {noun}"


def _show(value: object | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _abort(error: Exception) -> None:
    Console(stderr=True).print(f"[bold red]Error:[/] {error}")
    raise typer.Exit(code=1)
