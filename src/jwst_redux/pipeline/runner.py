"""Execute the official Detector1 pipeline for one selected archive product."""

from __future__ import annotations

import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from ..exceptions import PipelineExecutionError

PipelineCallable = Callable[[Path, Path, dict[str, Any]], None]


@dataclass(frozen=True)
class Stage1Result:
    outputs: tuple[Path, ...]
    elapsed_seconds: float
    log_path: Path


def resolve_crds_context(requested: str) -> str:
    """Resolve a configured CRDS context to the concrete JWST pipeline mapping."""
    import crds

    if requested in {"", "auto"}:
        return str(crds.get_default_context(observatory="jwst"))
    if requested == "latest":
        return str(crds.get_default_context(observatory="jwst", state="latest"))
    if not requested.endswith(".pmap"):
        raise PipelineExecutionError(
            "pipeline.crds_context must be 'auto', 'latest', or a concrete .pmap name."
        )
    return requested


def expected_stage1_outputs(input_path: Path, output_dir: Path) -> tuple[Path, ...]:
    """Return the Detector1 exposure- and integration-level output paths."""
    suffix = "_uncal.fits"
    if not input_path.name.endswith(suffix):
        raise PipelineExecutionError(f"Stage 1 input is not an _uncal FITS file: {input_path}")
    stem = input_path.name[: -len(suffix)]
    return (output_dir / f"{stem}_rate.fits", output_dir / f"{stem}_rateints.fits")


def existing_stage1_outputs(input_path: Path, output_dir: Path) -> tuple[Path, ...]:
    """Return required outputs plus any non-empty calibrated ramp that was saved."""
    required = expected_stage1_outputs(input_path, output_dir)
    stem = input_path.name.removesuffix("_uncal.fits")
    optional = (output_dir / f"{stem}_ramp.fits",)
    return required + tuple(
        path for path in optional if path.is_file() and path.stat().st_size > 0
    )


def run_detector1(
    input_path: Path,
    output_dir: Path,
    log_path: Path,
    crds_context: str,
    parameter_overrides: dict[str, Any],
    *,
    pipeline: PipelineCallable | None = None,
) -> Stage1Result:
    """Run Detector1 with official defaults and capture its complete console log."""
    required_outputs = expected_stage1_outputs(input_path, output_dir)
    started = time.monotonic()
    with (
        log_path.open("a", encoding="utf-8", buffering=1) as log_file,
        _tee_console(log_file),
        _temporary_crds_context(crds_context),
    ):
        print(f"jwst-redux: Detector1Pipeline input={input_path}")
        print(f"jwst-redux: CRDS context={crds_context}")
        try:
            (pipeline or _call_official_detector1)(input_path, output_dir, parameter_overrides)
        except Exception:
            traceback.print_exc()
            raise
    elapsed = time.monotonic() - started

    missing = [
        path
        for path in required_outputs
        if not path.is_file() or path.stat().st_size <= 0
    ]
    if missing:
        raise PipelineExecutionError(
            "Detector1Pipeline did not create the expected non-empty output(s): "
            + ", ".join(str(path) for path in missing)
        )
    return Stage1Result(
        outputs=existing_stage1_outputs(input_path, output_dir),
        elapsed_seconds=elapsed,
        log_path=log_path,
    )


def _call_official_detector1(
    input_path: Path, output_dir: Path, parameter_overrides: dict[str, Any]
) -> None:
    from jwst.pipeline import Detector1Pipeline

    result = Detector1Pipeline.call(
        str(input_path),
        output_dir=str(output_dir),
        save_results=True,
        **parameter_overrides,
    )
    if hasattr(result, "close"):
        result.close()


@contextmanager
def _temporary_crds_context(context: str) -> Iterator[None]:
    previous = os.environ.get("CRDS_CONTEXT")
    os.environ["CRDS_CONTEXT"] = context
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CRDS_CONTEXT", None)
        else:
            os.environ["CRDS_CONTEXT"] = previous


@contextmanager
def _tee_console(log_file: TextIO) -> Iterator[None]:
    stdout = _Tee(sys.stdout, log_file)
    stderr = _Tee(sys.stderr, log_file)
    with redirect_stdout(stdout), redirect_stderr(stderr):
        yield


class _Tee:
    def __init__(self, terminal: TextIO, log_file: TextIO) -> None:
        self.terminal = terminal
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.terminal.write(text)
        self.log_file.write(text)
        return len(text)

    def flush(self) -> None:
        self.terminal.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return self.terminal.encoding or "utf-8"
