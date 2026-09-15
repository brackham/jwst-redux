from pathlib import Path

import pytest

from jwst_redux.config import (
    load_batch_config,
    load_config,
    load_discovery_config,
    load_write_config,
)
from jwst_redux.exceptions import ConfigurationError


def test_validate_public_nirspec_batch_config() -> None:
    path = Path(__file__).parents[1] / "configs" / "toi3884-bots-g395h.yaml"
    g395h = load_batch_config(path)

    assert (g395h.discovery.query.instrument, g395h.discovery.query.exposure_type) == (
        "NIRSPEC",
        "NRS_BRIGHTOBJ",
    )
    assert g395h.discovery.query.observation_id == "003"
    assert g395h.discovery.query.proposal_ids == ("5799",)
    assert g395h.endpoint == "planned"
    assert g395h.failure_policy == "continue"


def test_nirspec_explicit_write_selection_requires_detector(tmp_path: Path) -> None:
    config_path = tmp_path / "nirspec.yaml"
    config_path.write_text(
        """
query:
  target: target
  instrument: NIRSPEC
  exposure_type: NRS_BRIGHTOBJ
products:
  start_from: uncal
pipeline:
  stages: auto
output:
  root: ./work
stage1:
  selection:
    program_id: 1
    observation_id: 1
    visit_number: 1
    exposure_number: 00001
    exposure_id: jw00001001001_00001_00001
options: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="requires stage1.selection.detector"):
        load_write_config(config_path)


def test_reject_unimplemented_target_matching_strategy(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
query:
  target: TOI-3884
  target_match: coordinates
  instrument: NIRISS
  exposure_type: NIS_SOSS
products:
  start_from: uncal
pipeline:
  stages: auto
output:
  root: ./work
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="coordinate and resolved-name"):
        load_discovery_config(config_path)


def test_non_mapping_configuration_raises_type_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(TypeError, match="YAML mapping"):
        load_config(config_path)


def test_proposal_ids_accept_one_or_multiple_values(tmp_path: Path) -> None:
    base = """
query:
  target: TOI-3884
  target_match: mast_targname
  archive_target_names: [TOI-3884, TOI-3884b]
  instrument: NIRISS
  exposure_type: NIS_SOSS
  proposal_ids: {proposal_ids}
products:
  start_from: uncal
pipeline:
  stages: auto
output:
  root: ./work
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(base.format(proposal_ids="5799"), encoding="utf-8")
    assert load_discovery_config(config_path).query.proposal_ids == ("5799",)

    config_path.write_text(base.format(proposal_ids="[5799, 5863]"), encoding="utf-8")
    assert load_discovery_config(config_path).query.proposal_ids == ("5799", "5863")
