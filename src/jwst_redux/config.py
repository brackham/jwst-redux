"""Configuration loading for jwst-redux."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigurationError


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


@dataclass(frozen=True)
class WriteConfig:
    """Validated configuration for selected-exposure pipeline execution."""

    discovery: DiscoveryConfig
    selection: ExposureSelectionConfig
    crds_context: str
    parameter_overrides: dict[str, Any]
    spec2_parameter_overrides: dict[str, Any]
    overwrite: bool


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

    overrides = pipeline.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigurationError("Configuration field 'pipeline.overrides' must be a mapping.")
    spec2_overrides = pipeline.get("spec2_overrides", {})
    if not isinstance(spec2_overrides, dict):
        raise ConfigurationError(
            "Configuration field 'pipeline.spec2_overrides' must be a mapping."
        )
    overwrite = options.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ConfigurationError("Configuration field 'options.overwrite' must be boolean.")

    return WriteConfig(
        discovery=discovery,
        selection=ExposureSelectionConfig(
            program_id=_required_identifier(selection, "program_id", width=5),
            observation_id=_required_identifier(selection, "observation_id", width=3),
            visit_number=_required_identifier(selection, "visit_number", width=3),
            exposure_id=_required_string(selection, "exposure_id"),
        ),
        crds_context=str(pipeline.get("crds_context", "auto")).strip().lower(),
        parameter_overrides=dict(overrides),
        spec2_parameter_overrides=dict(spec2_overrides),
        overwrite=overwrite,
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


def _required_identifier(data: dict[str, Any], key: str, *, width: int) -> str:
    value = data.get(key)
    identifier = _optional_identifier(value)
    if identifier is None:
        raise ConfigurationError(f"Configuration field '{key}' is required.")
    return identifier.zfill(width)


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
