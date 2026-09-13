"""Manifest-backed, independently resumable QA generation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import WriteConfig
from ..exceptions import JWSTReduxError
from ..modes import BOTS_MODE, SOSS_MODE
from ..provenance import ManifestStore, make_run_key, utc_now
from ..workspace import Workspace
from . import QA_SCHEMA_VERSION
from . import nirspec as nirspec_qa
from . import stage1 as stage1_qa
from . import stage2 as stage2_qa
from . import stage3 as stage3_qa
from .checks import write_extracted_spectrum_report
from .common import (
    F_LAMBDA_UNIT,
    SPECTRAL_QA_CONFIG,
    SPECTROSCOPIC_TIME_SERIES_CONFIG,
    qa_subdirectory,
)

SOSS_CHANNEL_CLASSIFICATION: dict[str, str] = {
    "validity": (
        "finite temporal coverage and finite, stable temporal-median normalization; "
        "invalid samples render neutrally"
    ),
    "science_quality": (
        "valid channels whose 99th-percentile temporal residual is at most 250 ppt; "
        "classification only, not the default display mask"
    ),
}

QA_PARAMETER_BASE: dict[str, Any] = {
    "stage1": {"image_limits": "finite 1st/99th percentiles", "scatter": "1.4826 * MAD"},
    "stage2": {
        "white_light": "sum finite DQ=0 extracted samples",
        "spectra": "native spectral density converted to F_lambda [W m^-2 um^-1] for display",
        "spectroscopic_time_series": {
            "quantity": "ppt relative to temporal median",
            **SPECTROSCOPIC_TIME_SERIES_CONFIG.as_provenance(),
        },
        "soss_channel_classification": SOSS_CHANNEL_CLASSIFICATION,
        "scatter_spectrum": {
            "quantity": "ppt relative temporal scatter",
            "estimator": "1.4826 * MAD_t(flux) / abs(median_t(flux))",
        },
        "point_to_point_difference": {
            "quantity": "ppt relative consecutive-integration difference",
            "formula": "1000 * (F(t_i) - F(t_i-1)) / median_t(F)",
            "time_coordinate": "timestamp of later integration t_i",
            "rows": "one fewer than the input integration count",
        },
    },
    "stage3": {
        "white_light": "official TSO3 whtlt.ecsv",
        "spectra": "native spectral density converted to F_lambda [W m^-2 um^-1] for display",
        "spectroscopic_time_series": {
            "quantity": "ppt relative to temporal median",
            **SPECTROSCOPIC_TIME_SERIES_CONFIG.as_provenance(),
        },
        "soss_channel_classification": SOSS_CHANNEL_CLASSIFICATION,
        "scatter_spectrum": {
            "quantity": "ppt relative temporal scatter",
            "estimator": "1.4826 * MAD_t(flux) / abs(median_t(flux))",
        },
        "point_to_point_difference": {
            "quantity": "ppt relative consecutive-integration difference",
            "formula": "1000 * (F(t_i) - F(t_i-1)) / median_t(F)",
            "time_coordinate": "timestamp of later integration t_i",
            "rows": "one fewer than the input integration count",
        },
    },
}


@dataclass(frozen=True)
class QAResult:
    stage: str
    status: str
    outputs: tuple[Path, ...]
    input_paths: tuple[Path, ...]
    upstream_run_ids: tuple[str, ...]
    manifest_entry: dict[str, Any]


def generate_qa(
    config: WriteConfig,
    *,
    stages: tuple[str, ...] = ("stage1", "stage2", "stage3"),
    force: bool = False,
) -> tuple[QAResult, ...]:
    """Build missing/stale QA from successful pipeline products only.

    This deliberately reads the manifest and existing products; it never invokes
    archive discovery, CRDS, or a JWST calibration pipeline.
    """
    unknown = set(stages).difference({"stage1", "stage2", "stage3"})
    if unknown:
        raise JWSTReduxError(f"Unsupported QA stage(s): {', '.join(sorted(unknown))}")
    workspace = Workspace.existing_for_selection(
        config.discovery.output_root.resolve(), config.selection
    )
    if not workspace.manifest.is_file():
        raise JWSTReduxError(f"Cannot generate QA without a manifest: {workspace.manifest}")
    workspace.create()
    manifest = ManifestStore(workspace.manifest, config.discovery.query.target)
    results: list[QAResult] = []
    for stage in stages:
        for pipeline_entry, inputs in _pipeline_products(manifest, config, stage):
            plotting_parameters = _qa_parameters(config, stage)
            results.append(
                _generate_one(
                    manifest,
                    workspace,
                    _selection_record(config),
                    plotting_parameters,
                    stage,
                    pipeline_entry,
                    inputs,
                    force,
                )
            )
    if not results:
        raise JWSTReduxError("No successful pipeline products matched the selected dataset/stage.")
    return tuple(results)


def _pipeline_products(
    manifest: ManifestStore, config: WriteConfig, stage: str
) -> list[tuple[dict[str, Any], tuple[Path, ...]]]:
    """Choose newest successful pipeline records, one per relevant segment."""
    operation = stage
    candidates = [
        entry
        for entry in manifest.entries()
        if entry.get("operation") == operation
        and entry.get("status") == "success"
        and entry.get("selection", _selection_record(config)) == _selection_record(config)
        and entry.get("exposure_identifier") == config.selection.exposure_id
    ]
    selected: dict[str, dict[str, Any]] = {}
    for entry in candidates:
        key = "visit" if stage == "stage3" else f"segment-{entry.get('segment_number')}"
        selected[key] = entry
    output: list[tuple[dict[str, Any], tuple[Path, ...]]] = []
    for entry in selected.values():
        paths = _required_paths(entry, stage)
        if paths is not None:
            output.append((entry, paths))
    return output


def _required_paths(entry: dict[str, Any], stage: str) -> tuple[Path, ...] | None:
    wanted = {
        "stage1": ("rateints",),
        "stage2": ("x1dints",),
        "stage3": ("x1dints", "whtlt"),
    }[stage]
    matches: list[Path] = []
    records = entry.get("outputs", [])
    if not isinstance(records, list):
        return None
    for suffix in wanted:
        found = [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("path", "")).endswith(
                f"_{suffix}.fits" if suffix != "whtlt" else "_whtlt.ecsv"
            )
        ]
        if len(found) != 1:
            return None
        record = found[0]
        path = Path(str(record["path"]))
        if not path.is_file() or path.stat().st_size != record.get("size_bytes"):
            return None
        matches.append(path)
    return tuple(matches)


def _generate_one(
    manifest: ManifestStore,
    workspace: Workspace,
    selection: dict[str, str],
    plotting_parameters: dict[str, Any],
    stage: str,
    pipeline_entry: dict[str, Any],
    inputs: tuple[Path, ...],
    force: bool,
) -> QAResult:
    product = inputs[0]
    output_dir = qa_subdirectory(workspace.qa(stage), product)
    input_records = [_file_record(path) for path in inputs]
    run_key = make_run_key(
        {
            "operation": "qa",
            "stage": stage,
            "qa_schema_version": QA_SCHEMA_VERSION,
            "inputs": input_records,
            "upstream_run_ids": [pipeline_entry["run_id"]],
            "plotting_parameters": plotting_parameters,
        }
    )
    run_id = str(uuid.uuid4())
    entry = {
        "run_id": run_id,
        "run_key": run_key,
        "operation": "qa",
        "selection": selection,
        "stage": stage,
        "status": "running",
        "start_time": utc_now(),
        "end_time": None,
        "qa_schema_version": QA_SCHEMA_VERSION,
        "input_pipeline_products": input_records,
        "upstream_successful_run_ids": [pipeline_entry["run_id"]],
        "plotting_parameters": plotting_parameters,
        "output_directory": str(output_dir),
    }
    manifest.append(entry)
    previous = None if force else manifest.successful_run(run_key)
    if previous is not None:
        manifest.update(
            run_id,
            {
                "status": "skipped",
                "end_time": utc_now(),
                "outputs": previous["outputs"],
                "resumed_from_run_id": previous["run_id"],
            },
        )
        _set_pipeline_qa_status(manifest, pipeline_entry["run_id"], "success", run_id)
        return QAResult(
            stage,
            "skipped",
            tuple(Path(x["path"]) for x in previous["outputs"]),
            inputs,
            (pipeline_entry["run_id"],),
            manifest.entry(run_id) or entry,
        )
    try:
        outputs = _plot(stage, inputs, output_dir, plotting_parameters)
        records = [_file_record(path) for path in outputs]
        manifest.update(run_id, {"status": "success", "end_time": utc_now(), "outputs": records})
        _set_pipeline_qa_status(manifest, pipeline_entry["run_id"], "success", run_id)
        return QAResult(
            stage,
            "success",
            outputs,
            inputs,
            (pipeline_entry["run_id"],),
            manifest.entry(run_id) or entry,
        )
    except Exception as error:  # noqa: BLE001 - QA must never invalidate calibration success.
        manifest.update(
            run_id, {"status": "failed", "end_time": utc_now(), "error": repr(error), "outputs": []}
        )
        _set_pipeline_qa_status(manifest, pipeline_entry["run_id"], "failed", run_id)
        return QAResult(
            stage,
            "failed",
            (),
            inputs,
            (pipeline_entry["run_id"],),
            manifest.entry(run_id) or entry,
        )


def _plot(
    stage: str,
    inputs: tuple[Path, ...],
    output_dir: Path,
    plotting_parameters: dict[str, Any],
) -> tuple[Path, ...]:
    if stage == "stage1":
        return stage1_qa.generate(inputs[0], output_dir)
    mode = (
        str(plotting_parameters["instrument"]),
        str(plotting_parameters["exposure_type"]),
    )
    if mode == BOTS_MODE:
        plots = nirspec_qa.generate(
            inputs[0],
            output_dir,
            stage=stage.replace("stage", "Stage "),
            white_light_path=inputs[1] if stage == "stage3" else None,
        )
        report = write_extracted_spectrum_report(
            inputs[0],
            output_dir / "checks.json",
            expected_instrument=mode[0],
            expected_exposure_type=mode[1],
            expected_detector=plotting_parameters.get("detector"),
        )
        return (*plots, report)
    if mode != SOSS_MODE:
        raise JWSTReduxError(f"No extracted-spectrum QA implementation for {mode[0]}/{mode[1]}.")
    if stage == "stage2":
        plots = stage2_qa.generate(inputs[0], output_dir, _wavelength_windows(plotting_parameters))
    else:
        plots = stage3_qa.generate(
            inputs[0], inputs[1], output_dir, _wavelength_windows(plotting_parameters)
        )
    report = write_extracted_spectrum_report(
        inputs[0],
        output_dir / "checks.json",
        expected_instrument=mode[0],
        expected_exposure_type=mode[1],
        expected_detector=None,
    )
    return (*plots, report)


def _file_record(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size_bytes": path.stat().st_size}


def _qa_parameters(config: WriteConfig, stage: str) -> dict[str, Any]:
    """Return generic and mode-specific QA provenance."""
    parameters = dict(QA_PARAMETER_BASE[stage])
    parameters.update(
        {
            "instrument": config.discovery.query.instrument,
            "exposure_type": config.discovery.query.exposure_type,
            "detector": config.selection.detector,
            "absolute_spectrum_unit": F_LAMBDA_UNIT.to_string(),
        }
    )
    mode = (config.discovery.query.instrument, config.discovery.query.exposure_type)
    if stage in {"stage2", "stage3"} and mode == SOSS_MODE:
        time_series = dict(parameters["spectroscopic_time_series"])
        time_series["wavelength_windows_microns"] = {
            str(order): list(window)
            for order, window in sorted(config.soss_wavelength_windows.items())
        }
        parameters["spectroscopic_time_series"] = time_series
    if stage in {"stage2", "stage3"} and mode == BOTS_MODE:
        parameters.pop("soss_channel_classification", None)
        parameters["spectral_qa_mask"] = SPECTRAL_QA_CONFIG.as_provenance()
        parameters["white_light"] = {
            "primary": (
                "median across QA-valid channels after normalization by each channel's "
                "temporal median; plotted as 1000 * (relative_flux - 1) ppt"
            ),
            "official_comparison": (
                "JWST WhiteLightStep wavelength sum, shown separately with an independent scale"
                if stage == "stage3"
                else "not available before TSO3"
            ),
        }
    return parameters


def _wavelength_windows(plotting_parameters: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Recover validated display windows from the recorded plotting parameters."""
    configured = plotting_parameters["spectroscopic_time_series"]["wavelength_windows_microns"]
    return {
        int(order): (float(bounds[0]), float(bounds[1])) for order, bounds in configured.items()
    }


def _selection_record(config: WriteConfig) -> dict[str, str]:
    """Match QA candidates to the complete selected exposure identity."""
    selection = config.selection
    record = {
        "program_id": selection.program_id,
        "observation_id": selection.observation_id,
        "visit_number": selection.visit_number,
        "exposure_number": selection.resolved_exposure_number,
        "exposure_id": selection.exposure_id,
        "label": selection.label,
        "workspace_name": selection.workspace_name,
    }
    if selection.detector is not None:
        record["detector"] = selection.detector
    return record


def _set_pipeline_qa_status(
    manifest: ManifestStore, pipeline_run_id: str, status: str, qa_run_id: str
) -> None:
    manifest.update(pipeline_run_id, {"qa_status": status, "qa_run_id": qa_run_id})
