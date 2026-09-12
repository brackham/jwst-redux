import json

from jwst_redux.config import ExposureSelectionConfig
from jwst_redux.workspace import Workspace


def test_workspace_create(tmp_path) -> None:
    workspace = Workspace(tmp_path / "example")
    workspace.create()
    assert workspace.raw.is_dir()
    assert workspace.stage1.is_dir()
    assert workspace.stage2.is_dir()
    assert workspace.stage3.is_dir()
    assert workspace.logs.is_dir()


def test_existing_workspace_uses_only_a_matching_legacy_manifest(tmp_path) -> None:
    selection = ExposureSelectionConfig(
        "05799", "001", "001", "jw05799001001_04101_00001", "04101"
    )
    legacy = Workspace(tmp_path / "legacy")
    legacy.create()
    legacy.manifest.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "scientific_dataset": {
                            "program_id": "05799",
                            "observation_id": "001",
                            "visit_number": "001",
                        },
                        "exposure_identifier": "jw05799001001_04101_00001",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert Workspace.existing_for_selection(tmp_path / "legacy", selection) == legacy
    other = ExposureSelectionConfig("05799", "002", "001", "jw05799002001_04101_00001", "04101")
    assert Workspace.existing_for_selection(tmp_path / "legacy", other) == Workspace.for_selection(
        tmp_path / "legacy", other
    )
