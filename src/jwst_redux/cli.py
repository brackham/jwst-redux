"""Command-line interface for jwst-redux."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import DiscoveryConfig, load_config, load_discovery_config
from .exceptions import JWSTReduxError
from .mast.query import DiscoveryResult, discover
from .models import DatasetPlan, Product, ReductionPlan, ScienceDataset
from .planning.resolver import make_reduction_plans
from .workspace import Workspace

app = typer.Typer(no_args_is_help=True, help="Retrieve and reduce JWST datasets reproducibly.")
console = Console()


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
def download(config: Path) -> None:
    """Download required archive products."""
    _config(config)
    console.print("[yellow]Not implemented yet:[/] MAST product download")


@app.command()
def run(config: Path) -> None:
    """Run the planned JWST calibration stages."""
    cfg = _config(config)
    root = Path(cfg["output"]["root"])
    Workspace(root).create()
    console.print(f"Workspace ready at {root}")
    console.print("[yellow]Not implemented yet:[/] JWST pipeline execution")


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
            console.print(
                f"  [bold]Exposure {exposure_number}: {_show(exposure.exposure_id)}[/]"
            )
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
        f"{_counted(len(plans), 'SOSS dataset')} containing "
        f"{_counted(len(result.exposures), 'exposure')}; "
        + _counted(
            reduction_count,
            "compatible reduction branch",
            "compatible reduction branches",
        )
    )
    for dataset_number, dataset_plan in enumerate(plans, start=1):
        dataset = dataset_plan.dataset
        console.print(
            f"\n[bold cyan]Dataset {dataset_number}: {_dataset_label(dataset)}[/]"
        )
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

    console.print(_product_summary(result.products, noun="_uncal archive input"))
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


def _search_summary(result: DiscoveryResult) -> str:
    sizes = _size_summary(result.products)
    return (
        f"Total: {_counted(len(result.datasets), 'SOSS dataset')} containing "
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
    console.print(f"[bold red]Error:[/] {error}", stderr=True)
    raise typer.Exit(code=1)
