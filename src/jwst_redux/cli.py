"""Command-line interface for jwst-redux."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .config import load_config
from .workspace import Workspace

app = typer.Typer(no_args_is_help=True, help="Retrieve and reduce JWST datasets reproducibly.")
console = Console()


def _config(path: Path) -> dict:
    return load_config(path)


@app.command()
def search(config: Path) -> None:
    """Search MAST for observations matching CONFIG."""
    _config(config)
    console.print("[yellow]Not implemented yet:[/] MAST observation search")


@app.command()
def plan(config: Path) -> None:
    """Show what would be downloaded and which pipeline stages would run."""
    _config(config)
    console.print("[yellow]Not implemented yet:[/] reduction planning")


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
