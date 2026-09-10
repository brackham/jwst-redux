from jwst_redux.models import Observation


def test_observation_is_hashable_frozen_dataclass() -> None:
    observation = Observation(
        program_id=None,
        observation_id=None,
        visit_id=None,
        target_name="TOI-3884",
        instrument="NIRISS",
        exposure_type="NIS_SOSS",
    )
    assert observation.target_name == "TOI-3884"
