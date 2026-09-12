from pathlib import Path

import pytest

from jwst_redux.config import load_config, load_discovery_config, load_write_config
from jwst_redux.exceptions import ConfigurationError


def test_load_toi3884_config() -> None:
    path = Path(__file__).parents[1] / "configs" / "toi3884.yaml"
    config = load_config(path)
    assert config["query"]["target"] == "TOI-3884"
    assert config["query"]["instrument"] == "NIRISS"
    assert config["query"]["exposure_type"] == "NIS_SOSS"


def test_validate_toi3884_discovery_config() -> None:
    path = Path(__file__).parents[1] / "configs" / "toi3884.yaml"
    config = load_discovery_config(path)
    assert config.query.target == "TOI-3884"
    assert config.query.target_match == "mast_targname"
    assert config.query.archive_target_names == ("TOI-3884", "TOI-3884b")
    assert config.query.proposal_ids is None
    assert config.start_from == "uncal"


def test_validate_toi3884_exposure_selection() -> None:
    path = Path(__file__).parents[1] / "configs" / "toi3884.yaml"
    config = load_write_config(path)
    assert config.selection.program_id == "05799"
    assert config.selection.observation_id == "002"
    assert config.selection.visit_number == "001"
    assert config.selection.exposure_number == "04101"
    assert config.selection.exposure_id == "jw05799002001_04101_00001"
    assert config.selection.label == "GO-5799 Obs 002 / Visit 001 / Exposure 04101"
    assert config.crds_context == "auto"
    assert config.parameter_overrides == {}
    assert config.spec2_parameter_overrides == {}
    assert config.tso3_parameter_overrides == {}
    assert config.soss_wavelength_windows == {1: (0.85, 2.83), 2: (0.60, 1.00), 3: (0.70, 0.95)}


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
