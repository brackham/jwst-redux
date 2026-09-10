from pathlib import Path

from jwst_redux.config import load_config


def test_load_toi3884_config() -> None:
    path = Path(__file__).parents[1] / "configs" / "toi3884.yaml"
    config = load_config(path)
    assert config["query"]["target"] == "TOI-3884"
    assert config["query"]["instrument"] == "NIRISS"
    assert config["query"]["exposure_type"] == "NIS_SOSS"
