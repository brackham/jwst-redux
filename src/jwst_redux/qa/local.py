"""Select existing fixed-slit workspaces for QA without archive or CRDS access."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import (
    BatchConfig,
    ExposureSelectionConfig,
    QueryConfig,
    WriteConfig,
    write_config_for_exposure,
)
from ..exceptions import JWSTReduxError, QAUnavailableError
from ..modes import FIXED_SLIT_MODE
from ..provenance import ManifestStore


@dataclass(frozen=True)
class LocalQABranch:
    config: WriteConfig
    label: str


@dataclass(frozen=True)
class LocalQABatch:
    branches: tuple[LocalQABranch, ...]


def _same_id(a, b) -> bool:
    return str(a).lstrip("0") == str(b).lstrip("0")


def open_group_manifest(path: Path) -> ManifestStore:
    """Use the historical query label; scientific selection uses group identity fields.

    A manifest label can be a proposal scope or an earlier target query. QA must
    not rewrite that label when a narrower or broader query selects this group.
    ManifestStore still validates the schema and the original label on every read.
    """
    try:
        return ManifestStore(path, json.loads(path.read_text())["scientific_target"])
    except (OSError, ValueError, KeyError) as error:
        raise JWSTReduxError(f"Cannot read group manifest {path}: {error}") from error


def _candidate_manifests(root: Path, query: QueryConfig) -> list[Path]:
    """Avoid hydrating unrelated canonical cloud workspaces for a narrow query.

    Directory names only prune impossible program/observation/visit candidates;
    every included group is still checked against its recorded scientific identity.
    Older/noncanonical names are always inspected.
    """
    candidates = []
    for path in sorted(root.resolve().glob("groups/*/manifest.json")):
        match = re.fullmatch(
            r"nirspec-nrs_fixedslit-(\d{5})-(\d{3})-(\d{3})-[0-9a-f]{16}", path.parent.name
        )
        if match:
            program, observation, visit = match.groups()
            if query.proposal_ids and not any(_same_id(program, p) for p in query.proposal_ids):
                continue
            if query.observation_id and not _same_id(observation, query.observation_id):
                continue
            if query.visit_id and not _same_id(program + observation + visit, query.visit_id):
                continue
        candidates.append(path)
    return candidates


def prepare_local_fixedslit_qa(config: BatchConfig) -> LocalQABatch:
    """Honor query filters against recorded group identity; never reconstruct associations."""
    query = config.discovery.query
    branches = []
    for path in _candidate_manifests(config.discovery.output_root, query):
        manifest = open_group_manifest(path)
        entries = [
            e
            for e in manifest.entries()
            if e.get("operation") in {"group", "stage3"}
            and e.get("group_id") == path.parent.name
            and e.get("compatibility")
        ]
        if not entries:
            continue
        entry = entries[-1]
        identity = entry["compatibility"]
        if (identity.get("instrument"), identity.get("exposure_type")) != FIXED_SLIT_MODE:
            continue
        if query.proposal_ids and not any(
            _same_id(identity.get("program_id"), p) for p in query.proposal_ids
        ):
            continue
        if query.observation_id and not _same_id(
            identity.get("observation_id"), query.observation_id
        ):
            continue
        visit = "".join(
            str(identity.get(k, "")).zfill(w)
            for k, w in (("program_id", 5), ("observation_id", 3), ("visit_number", 3))
        )
        if query.visit_id and not _same_id(visit, query.visit_id):
            continue
        if query.grating and identity.get("grating") != query.grating:
            continue
        if query.filter and identity.get("filter") != query.filter:
            continue
        targets = query.archive_target_names or ((query.target,) if query.target else ())
        if targets and str(identity.get("target_name", "")).casefold() not in {
            t.casefold() for t in targets
        }:
            continue
        exposures = entry.get("exposure_identifiers", [])
        if not exposures:
            continue
        selection = ExposureSelectionConfig(
            identity["program_id"],
            identity["observation_id"],
            identity["visit_number"],
            exposures[0],
        )
        selected = replace(write_config_for_exposure(config, selection), group_id=path.parent.name)
        label = f"{path.parent.name} / {identity.get('grating')} / {identity.get('fixed_slit')}"
        branches.append(LocalQABranch(selected, label))
    if not branches:
        raise QAUnavailableError(
            f"No recorded fixed-slit groups match the query under {config.discovery.output_root}"
        )
    return LocalQABatch(tuple(branches))


def resolve_selected_group(config: WriteConfig) -> WriteConfig:
    """Resolve an explicit exposure to exactly one existing group workspace."""
    if config.group_id:
        return config
    matches = []
    selection_query = replace(
        config.discovery.query,
        proposal_ids=(config.selection.program_id,),
        observation_id=config.selection.observation_id,
        visit_id=config.selection.program_id
        + config.selection.observation_id
        + config.selection.visit_number,
    )
    for path in _candidate_manifests(config.discovery.output_root, selection_query):
        for entry in open_group_manifest(path).entries():
            identity = entry.get("compatibility", {})
            if (
                entry.get("group_id") == path.parent.name
                and config.selection.exposure_id in entry.get("exposure_identifiers", [])
                and all(
                    _same_id(identity.get(k), getattr(config.selection, k))
                    for k in ("program_id", "observation_id", "visit_number")
                )
            ):
                matches.append(path.parent.name)
                break
    if len(matches) != 1:
        raise QAUnavailableError(
            "Selected exposure must identify exactly one recorded fixed-slit group"
        )
    return replace(config, group_id=matches[0])
