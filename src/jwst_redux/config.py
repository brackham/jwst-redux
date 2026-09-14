"""Configuration loading for jwst-redux."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigurationError

DEFAULT_SOSS_WAVELENGTH_WINDOWS: dict[int, tuple[float, float]] = {
    1: (0.85, 2.83),
    2: (0.60, 1.00),
    3: (0.70, 0.95),
}


@dataclass(frozen=True)
class QueryConfig:
    """Archive-independent target request plus its current MAST matching strategy."""

    target: str
    target_match: str
    archive_target_names: tuple[str, ...]
    instrument: str
    exposure_type: str
    proposal_ids: tuple[str, ...] | None = None
    observation_id: str | None = None
    visit_id: str | None = None


@dataclass(frozen=True)
class DiscoveryConfig:
    """Validated configuration needed by the read-only discovery and planning commands."""

    query: QueryConfig
    start_from: str
    output_root: Path


@dataclass(frozen=True)
class ExposureSelectionConfig:
    """Exact scientific dataset and exposure selected for sequential processing."""

    program_id: str
    observation_id: str
    visit_number: str
    exposure_id: str
    exposure_number: str | None = None
    detector: str | None = None

    @property
    def resolved_exposure_number(self) -> str:
        """Return the human-facing JWST exposure number for this selection."""
        if self.exposure_number is not None:
            return self.exposure_number
        parts = self.exposure_id.split("_")
        return parts[1] if len(parts) > 1 else self.exposure_id

    @property
    def label(self) -> str:
        """Human-readable identity for command output and provenance."""
        program = str(int(self.program_id)) if self.program_id.isdigit() else self.program_id
        label = (
            f"GO-{program} Obs {self.observation_id} / Visit {self.visit_number} "
            f"/ Exposure {self.resolved_exposure_number}"
        )
        return label if self.detector is None else f"{label} / Detector {self.detector}"

    @property
    def workspace_name(self) -> str:
        """Stable directory name unique to this complete selected exposure."""
        name = (
            f"go-{int(self.program_id):04d}-obs-{self.observation_id}"
            f"-visit-{self.visit_number}-exposure-{self.resolved_exposure_number}"
            f"-{self.exposure_id}"
        )
        return name if self.detector is None else f"{name}-detector-{self.detector.lower()}"


@dataclass(frozen=True)
class WriteConfig:
    """Validated configuration for selected-exposure pipeline execution."""

    discovery: DiscoveryConfig
    selection: ExposureSelectionConfig
    crds_context: str
    parameter_overrides: dict[str, Any]
    spec2_parameter_overrides: dict[str, Any]
    tso3_parameter_overrides: dict[str, Any]
    overwrite: bool
    qa_enabled: bool = False
    soss_wavelength_windows: dict[int, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_SOSS_WAVELENGTH_WINDOWS)
    )


@dataclass(frozen=True)
class BatchConfig:
    """Selector-free settings for sequential, planner-driven batch execution."""

    discovery: DiscoveryConfig
    endpoint: str
    crds_context: str
    parameter_overrides: dict[str, Any]
    spec2_parameter_overrides: dict[str, Any]
    tso3_parameter_overrides: dict[str, Any]
    overwrite: bool
    failure_policy: str
    qa_enabled: bool = False
    soss_wavelength_windows: dict[int, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_SOSS_WAVELENGTH_WINDOWS)
    )


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a YAML configuration file."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise TypeError(f"Configuration must contain a YAML mapping: {config_path}")
    return data


def load_discovery_config(path: str | Path) -> DiscoveryConfig:
    """Load and validate the subset used by ``search`` and ``plan``."""
    data = load_config(path)
    query = _mapping(data, "query")
    products = _mapping(data, "products")
    output = _mapping(data, "output")

    target_match = query.get("target_match", "mast_targname")
    if target_match != "mast_targname":
        raise ConfigurationError(
            "Only query.target_match='mast_targname' is implemented; "
            "coordinate and resolved-name target matching will be added later."
        )

    start_from = _required_string(products, "start_from").lower()
    if start_from != "uncal":
        raise ConfigurationError("This milestone only supports products.start_from='uncal'.")

    pipeline = _mapping(data, "pipeline")
    if pipeline.get("stages", "auto") != "auto":
        raise ConfigurationError("This milestone only supports pipeline.stages='auto'.")

    return DiscoveryConfig(
        query=QueryConfig(
            target=_required_string(query, "target"),
            target_match=target_match,
            archive_target_names=_archive_target_names(query),
            instrument=_required_string(query, "instrument").upper(),
            exposure_type=_required_string(query, "exposure_type").upper(),
            proposal_ids=_proposal_ids(query),
            observation_id=_optional_identifier(query.get("observation_id")),
            visit_id=_optional_identifier(query.get("visit_id")),
        ),
        start_from=start_from,
        output_root=Path(_required_string(output, "root")),
    )


def load_write_config(path: str | Path) -> WriteConfig:
    """Load the exact selected exposure and pipeline execution settings."""
    discovery = load_discovery_config(path)
    data = load_config(path)
    stage1 = _mapping(data, "stage1")
    selection = _mapping(stage1, "selection")
    pipeline = _mapping(data, "pipeline")
    options = _mapping(data, "options")
    qa = data.get("qa", {})
    if not isinstance(qa, dict):
        raise ConfigurationError("Configuration field 'qa' must be a mapping.")
    qa_enabled = qa.get("enabled", True)
    if not isinstance(qa_enabled, bool):
        raise ConfigurationError("Configuration field 'qa.enabled' must be boolean.")
    soss_wavelength_windows = _soss_wavelength_windows(qa)

    overrides = pipeline.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigurationError("Configuration field 'pipeline.overrides' must be a mapping.")
    spec2_overrides = pipeline.get("spec2_overrides", {})
    if not isinstance(spec2_overrides, dict):
        raise ConfigurationError(
            "Configuration field 'pipeline.spec2_overrides' must be a mapping."
        )
    tso3_overrides = pipeline.get("tso3_overrides", {})
    if not isinstance(tso3_overrides, dict):
        raise ConfigurationError("Configuration field 'pipeline.tso3_overrides' must be a mapping.")
    overwrite = options.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ConfigurationError("Configuration field 'options.overwrite' must be boolean.")

    exposure_id = _required_string(selection, "exposure_id")
    exposure_number = _required_identifier(selection, "exposure_number", width=5)
    detector = _optional_upper_string(selection.get("detector"))
    if discovery.query.instrument == "NIRSPEC" and detector not in {"NRS1", "NRS2"}:
        raise ConfigurationError(
            "NIRSpec write configuration requires stage1.selection.detector to be NRS1 or NRS2."
        )
    if _exposure_number_from_id(exposure_id) != exposure_number:
        raise ConfigurationError(
            "Configuration field 'stage1.selection.exposure_number' must match the "
            "exposure number encoded in 'exposure_id'."
        )

    return WriteConfig(
        discovery=discovery,
        selection=ExposureSelectionConfig(
            program_id=_required_identifier(selection, "program_id", width=5),
            observation_id=_required_identifier(selection, "observation_id", width=3),
            visit_number=_required_identifier(selection, "visit_number", width=3),
            exposure_id=exposure_id,
            exposure_number=exposure_number,
            detector=detector,
        ),
        crds_context=str(pipeline.get("crds_context", "auto")).strip().lower(),
        parameter_overrides=dict(overrides),
        spec2_parameter_overrides=dict(spec2_overrides),
        tso3_parameter_overrides=dict(tso3_overrides),
        overwrite=overwrite,
        qa_enabled=qa_enabled,
        soss_wavelength_windows=soss_wavelength_windows,
    )


def load_batch_config(path: str | Path) -> BatchConfig:
    """Load a selector-free batch config whose endpoint is planner controlled."""
    discovery = load_discovery_config(path)
    data = load_config(path)
    stage1 = data.get("stage1", {})
    if not isinstance(stage1, dict):
        raise ConfigurationError("Configuration field 'stage1' must be a mapping when supplied.")
    if "selection" in stage1:
        raise ConfigurationError(
            "Batch configuration must not contain 'stage1.selection'; use ordinary run "
            "without --all for an explicit selected exposure."
        )
    pipeline = _mapping(data, "pipeline")
    options = _mapping(data, "options")
    qa = data.get("qa", {})
    if not isinstance(qa, dict):
        raise ConfigurationError("Configuration field 'qa' must be a mapping.")
    qa_enabled = qa.get("enabled", True)
    if not isinstance(qa_enabled, bool):
        raise ConfigurationError("Configuration field 'qa.enabled' must be boolean.")
    endpoint = str(pipeline.get("endpoint", "planned")).strip().lower()
    if endpoint == "auto":
        endpoint = "planned"
    if endpoint != "planned":
        raise ConfigurationError("Batch pipeline.endpoint must be 'planned' or 'auto'.")
    failure_policy = str(options.get("batch_failure_policy", "continue")).strip().lower()
    if failure_policy not in {"continue", "stop"}:
        raise ConfigurationError("options.batch_failure_policy must be 'continue' or 'stop'.")
    overrides = pipeline.get("overrides", {})
    spec2_overrides = pipeline.get("spec2_overrides", {})
    tso3_overrides = pipeline.get("tso3_overrides", {})
    for field_name, value in (
        ("pipeline.overrides", overrides),
        ("pipeline.spec2_overrides", spec2_overrides),
        ("pipeline.tso3_overrides", tso3_overrides),
    ):
        if not isinstance(value, dict):
            raise ConfigurationError(f"Configuration field '{field_name}' must be a mapping.")
    overwrite = options.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ConfigurationError("Configuration field 'options.overwrite' must be boolean.")
    return BatchConfig(
        discovery=discovery,
        endpoint=endpoint,
        crds_context=str(pipeline.get("crds_context", "auto")).strip().lower(),
        parameter_overrides=dict(overrides),
        spec2_parameter_overrides=dict(spec2_overrides),
        tso3_parameter_overrides=dict(tso3_overrides),
        overwrite=overwrite,
        failure_policy=failure_policy,
        qa_enabled=qa_enabled,
        soss_wavelength_windows=_soss_wavelength_windows(qa),
    )


def write_config_for_exposure(
    config: BatchConfig, selection: ExposureSelectionConfig
) -> WriteConfig:
    """Materialize one isolated write configuration for a planned branch."""
    return WriteConfig(
        discovery=config.discovery,
        selection=selection,
        crds_context=config.crds_context,
        parameter_overrides=dict(config.parameter_overrides),
        spec2_parameter_overrides=dict(config.spec2_parameter_overrides),
        tso3_parameter_overrides=dict(config.tso3_parameter_overrides),
        overwrite=config.overwrite,
        qa_enabled=config.qa_enabled,
        soss_wavelength_windows=dict(config.soss_wavelength_windows),
    )


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration field '{key}' must be a mapping.")
    return value


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"Configuration field '{key}' must be a non-empty string.")
    return value.strip()


def _optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ConfigurationError("Optional archive identifiers cannot be empty strings.")
    return text


def _optional_upper_string(value: Any) -> str | None:
    text = _optional_identifier(value)
    return text.upper() if text is not None else None


def _required_identifier(data: dict[str, Any], key: str, *, width: int) -> str:
    value = data.get(key)
    identifier = _optional_identifier(value)
    if identifier is None:
        raise ConfigurationError(f"Configuration field '{key}' is required.")
    return identifier.zfill(width)


def _exposure_number_from_id(exposure_id: str) -> str | None:
    """Extract the five-digit exposure number from a JWST exposure identifier."""
    parts = exposure_id.split("_")
    if len(parts) < 2 or len(parts[1]) != 5 or not parts[1].isdigit():
        return None
    return parts[1]


def _soss_wavelength_windows(qa: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """Validate optional display windows for NIRISS/SOSS spectroscopic time series."""
    soss = qa.get("soss", {})
    if not isinstance(soss, dict):
        raise ConfigurationError("Configuration field 'qa.soss' must be a mapping.")
    configured = soss.get("wavelength_windows", {})
    if not isinstance(configured, dict):
        raise ConfigurationError(
            "Configuration field 'qa.soss.wavelength_windows' must be a mapping."
        )
    windows = dict(DEFAULT_SOSS_WAVELENGTH_WINDOWS)
    for order_key, bounds in configured.items():
        try:
            order = int(order_key)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                "SOSS wavelength-window order keys must be positive integers."
            ) from error
        if order <= 0 or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ConfigurationError(
                "Each SOSS wavelength window must be a two-value [minimum, maximum] list."
            )
        try:
            lower, upper = (float(value) for value in bounds)
        except (TypeError, ValueError) as error:
            raise ConfigurationError("SOSS wavelength-window bounds must be numeric.") from error
        if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
            raise ConfigurationError(
                "SOSS wavelength-window bounds must be finite and strictly increasing."
            )
        windows[order] = (lower, upper)
    return windows


def _archive_target_names(query: dict[str, Any]) -> tuple[str, ...]:
    target = _required_string(query, "target")
    value = query.get("archive_target_names")
    if value is None:
        return (target,)
    return _identifier_list(value, "query.archive_target_names")


def _proposal_ids(query: dict[str, Any]) -> tuple[str, ...] | None:
    singular_present = query.get("proposal_id") is not None
    plural_present = query.get("proposal_ids") is not None
    if singular_present and plural_present:
        raise ConfigurationError("Use only one of query.proposal_id or query.proposal_ids.")
    value = query.get("proposal_ids") if "proposal_ids" in query else query.get("proposal_id")
    if value is None:
        return None
    return _identifier_list(value, "query.proposal_ids")


def _identifier_list(value: Any, field: str) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ConfigurationError(f"Configuration field '{field}' cannot be an empty list.")
    identifiers = tuple(_optional_identifier(item) for item in values)
    if any(identifier is None for identifier in identifiers):
        raise ConfigurationError(f"Configuration field '{field}' cannot contain null values.")
    return tuple(str(identifier) for identifier in identifiers)
