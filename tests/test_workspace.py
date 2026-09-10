from jwst_redux.workspace import Workspace


def test_workspace_create(tmp_path) -> None:
    workspace = Workspace(tmp_path / "example")
    workspace.create()
    assert workspace.raw.is_dir()
    assert workspace.stage1.is_dir()
    assert workspace.stage2.is_dir()
    assert workspace.stage3.is_dir()
    assert workspace.logs.is_dir()
