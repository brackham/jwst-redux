from jwst_redux.models import Exposure


def test_exposure_preserves_archive_metadata() -> None:
    exposure = Exposure(
        program_id=None,
        observation_id=None,
        visit_id=None,
        target_name="TOI-3884",
        instrument="NIRISS",
        exposure_type="NIS_SOSS",
    )
    assert exposure.target_name == "TOI-3884"
