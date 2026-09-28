"""CLI coverage for manifest-only batch QA regeneration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.exceptions import QAUnavailableError
from jwst_redux.qa.workflow import QAResult


def _branch(label: str, config: object) -> SimpleNamespace:
    return SimpleNamespace(label=label, config=config)


def _qa_result(
    tmp_path: Path,
    *,
    stage: str = "stage3",
    status: str = "success",
    name: str = "qa.png",
) -> QAResult:
    output = tmp_path / name
    error = {"error": "synthetic plot failure"} if status == "failed" else {}
    return QAResult(
        stage,
        status,
        () if status == "failed" else (output,),
        (tmp_path / f"{stage}_input.fits",),
        ("pipeline-run",),
        error,
    )


def _forbid(name: str):
    def forbidden(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called by batch QA")

    return forbidden


def test_selected_qa_still_uses_write_config(monkeypatch, tmp_path: Path) -> None:
    selected = SimpleNamespace(selection=SimpleNamespace(label="selected exposure"))
    calls: list[tuple[object, tuple[str, ...], bool]] = []
    monkeypatch.setattr(cli, "load_write_config", lambda _: selected)
    monkeypatch.setattr(cli, "load_batch_config", _forbid("load_batch_config"))

    def generate(config, *, stages, force):
        calls.append((config, stages, force))
        return (_qa_result(tmp_path, stage="stage1"),)

    monkeypatch.setattr(cli, "generate_qa", generate)

    result = CliRunner().invoke(cli.app, ["qa", "selected.yaml", "--stage", "stage1"])

    assert result.exit_code == 0
    assert calls == [(selected, ("stage1",), False)]
    assert "Selected: selected exposure" in result.stdout


def test_qa_all_uses_prepared_branch_configs_and_skips_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    batch_config = object()
    first_config = object()
    second_config = object()
    prepared = SimpleNamespace(
        branches=(
            _branch("branch one", first_config),
            _branch("branch two", second_config),
        )
    )
    calls: list[tuple[object, tuple[str, ...], bool]] = []
    monkeypatch.setattr(cli, "load_batch_config", lambda _: batch_config)
    monkeypatch.setattr(cli, "load_write_config", _forbid("load_write_config"))
    monkeypatch.setattr(
        cli,
        "prepare_batch",
        lambda config: prepared if config is batch_config else _forbid("unexpected config")(),
    )
    monkeypatch.setattr(cli, "run_batch", _forbid("run_batch"))
    monkeypatch.setattr(cli, "run_selected_through", _forbid("run_selected_through"))
    monkeypatch.setattr(cli, "download_selected", _forbid("download_selected"))

    def generate(config, *, stages, force):
        calls.append((config, stages, force))
        if config is second_config:
            raise QAUnavailableError("no successful Stage 3 product")
        return (_qa_result(tmp_path, name="branch-one.png"),)

    monkeypatch.setattr(cli, "generate_qa", generate)

    result = CliRunner().invoke(
        cli.app,
        ["qa", "batch.yaml", "--all", "--stage", "stage3", "--force"],
    )

    assert result.exit_code == 0
    assert calls == [
        (first_config, ("stage3",), True),
        (second_config, ("stage3",), True),
    ]
    assert "QA Stage 3 success: branch one" in result.stdout
    assert "branch-one.png" in result.stdout
    assert "QA Stage 3 unavailable: branch two" in result.stdout


def test_qa_all_propagates_repeated_stages(monkeypatch, tmp_path: Path) -> None:
    branch_config = object()
    prepared = SimpleNamespace(branches=(_branch("both stages", branch_config),))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(cli, "load_batch_config", lambda _: object())
    monkeypatch.setattr(cli, "prepare_batch", lambda _: prepared)

    def generate(config, *, stages, force):
        assert config is branch_config and force is False
        calls.append(stages)
        return (
            _qa_result(tmp_path, stage="stage1", name="stage1.png"),
            _qa_result(tmp_path, stage="stage3", name="stage3.png"),
        )

    monkeypatch.setattr(cli, "generate_qa", generate)

    result = CliRunner().invoke(
        cli.app,
        ["qa", "batch.yaml", "--all", "--stage", "stage1", "--stage", "stage3"],
    )

    assert result.exit_code == 0
    assert calls == [("stage1", "stage3")]
    assert "QA Stage 1 success: both stages" in result.stdout
    assert "QA Stage 3 success: both stages" in result.stdout


def test_qa_all_continues_after_failure_and_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    failed_config = object()
    successful_config = object()
    prepared = SimpleNamespace(
        branches=(
            _branch("bad branch", failed_config),
            _branch("good branch", successful_config),
        )
    )
    calls: list[object] = []
    monkeypatch.setattr(cli, "load_batch_config", lambda _: object())
    monkeypatch.setattr(cli, "prepare_batch", lambda _: prepared)

    def generate(config, *, stages, force):
        calls.append(config)
        assert stages == ("stage3",) and force is True
        if config is failed_config:
            return (_qa_result(tmp_path, status="failed"),)
        return (_qa_result(tmp_path, name="successful-sibling.png"),)

    monkeypatch.setattr(cli, "generate_qa", generate)

    result = CliRunner().invoke(
        cli.app,
        ["qa", "batch.yaml", "--all", "--stage", "stage3", "--force"],
    )

    assert result.exit_code == 1
    assert calls == [failed_config, successful_config]
    assert "QA Stage 3 failed: bad branch: synthetic plot failure" in result.stdout
    assert "QA Stage 3 success: good branch" in result.stdout
    assert "successful-sibling.png" in result.stdout


def test_qa_all_continues_after_unexpected_branch_error(monkeypatch, tmp_path: Path) -> None:
    broken_config = object()
    successful_config = object()
    prepared = SimpleNamespace(
        branches=(
            _branch("broken branch", broken_config),
            _branch("later branch", successful_config),
        )
    )
    calls: list[object] = []
    monkeypatch.setattr(cli, "load_batch_config", lambda _: object())
    monkeypatch.setattr(cli, "prepare_batch", lambda _: prepared)

    def generate(config, *, stages, force):
        calls.append(config)
        if config is broken_config:
            raise RuntimeError("unexpected QA error")
        return (_qa_result(tmp_path, name="later.png"),)

    monkeypatch.setattr(cli, "generate_qa", generate)

    result = CliRunner().invoke(cli.app, ["qa", "batch.yaml", "--all", "--stage", "stage3"])

    assert result.exit_code == 1
    assert calls == [broken_config, successful_config]
    assert "QA Stage 3 failed: broken branch: unexpected QA error" in result.stdout
    assert "QA Stage 3 success: later branch" in result.stdout


def test_batch_config_without_all_has_actionable_error(tmp_path: Path) -> None:
    config = tmp_path / "batch.yaml"
    config.write_text(
        f"""
query:
  target: TOI-3884
  target_match: mast_targname
  instrument: NIRISS
  exposure_type: NIS_SOSS
products:
  start_from: uncal
pipeline:
  stages: auto
  endpoint: planned
output:
  root: {tmp_path / "work"}
options:
  overwrite: false
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["qa", str(config), "--stage", "stage3"])

    assert result.exit_code == 1
    assert "This is a batch configuration" in result.stderr
    assert "jwst-redux qa CONFIG --all" in result.stderr
