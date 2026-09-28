from pathlib import Path

import pytest

from jwst_redux.config import (
    DEFAULT_QUICKLOOK_CADENCE_MINUTES,
    load_batch_config,
    load_config,
    load_discovery_config,
    load_write_config,
)
from jwst_redux.exceptions import ConfigurationError


def _write_discovery_config(path: Path, output_root: str) -> None:
    path.write_text(
        f"""
query:
  target: TOI-3884
  instrument: NIRISS
  exposure_type: NIS_SOSS
products:
  start_from: uncal
pipeline:
  stages: auto
output:
  root: {output_root}
""",
        encoding="utf-8",
    )


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
    assert g395h.retention == "all"
    assert g395h.quicklook_cadence_minutes == DEFAULT_QUICKLOOK_CADENCE_MINUTES


def test_quicklook_cadence_can_be_overridden_in_yaml(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "configs" / "toi3884-bots-g395h.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        source.read_text(encoding="utf-8").replace(
            "qa:\n  enabled: true", "qa:\n  enabled: true\n  quicklook:\n    cadence_minutes: 3.5"
        ),
        encoding="utf-8",
    )

    assert load_batch_config(config_path).quicklook_cadence_minutes == 3.5


@pytest.mark.parametrize("value", ["0", "-1", ".nan", ".inf", "false", "not-a-number"])
def test_invalid_quicklook_cadence_fails_clearly(tmp_path: Path, value: str) -> None:
    source = Path(__file__).parents[1] / "configs" / "toi3884-bots-g395h.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        source.read_text(encoding="utf-8").replace(
            "qa:\n  enabled: true",
            f"qa:\n  enabled: true\n  quicklook:\n    cadence_minutes: {value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=r"qa\.quicklook\.cadence_minutes.*positive"):
        load_batch_config(config_path)


def test_output_root_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.yaml"
    _write_discovery_config(config_path, "~/Desktop/toi3884")

    assert load_discovery_config(config_path).output_root == home / "Desktop" / "toi3884"


def test_relative_output_root_remains_relative(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    _write_discovery_config(config_path, "work/toi3884")

    output_root = load_discovery_config(config_path).output_root

    assert output_root == Path("work/toi3884")
    assert not output_root.is_absolute()


def test_invalid_batch_retention_value_fails_clearly(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "configs" / "toi3884-bots-g395h.yaml"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        source.read_text(encoding="utf-8").replace("retention: all", "retention: minimal"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=r"options\.retention.*'all' or 'final'"):
        load_batch_config(config_path)

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("retention: minimal", "retention: final"),
        encoding="utf-8",
    )
    assert load_batch_config(config_path).retention == "final"


def test_invalid_write_retention_value_fails_clearly(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contents = """
query:
  target: TOI-3884
  instrument: NIRISS
  exposure_type: NIS_SOSS
products:
  start_from: uncal
pipeline:
  stages: auto
output:
  root: ./work
stage1:
  selection:
    program_id: 5799
    observation_id: 1
    visit_number: 1
    exposure_number: "04101"
    exposure_id: jw05799001001_04101_00001
options:
  retention: minimal
"""
    config_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"options\.retention.*'all' or 'final'"):
        load_write_config(config_path)

    config_path.write_text(
        contents.replace("retention: minimal", "retention: final"), encoding="utf-8"
    )
    assert load_write_config(config_path).retention == "final"
    config_path.write_text(
        contents.replace("options:\n  retention: minimal", "options: {}"), encoding="utf-8"
    )
    assert load_write_config(config_path).retention == "all"


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
