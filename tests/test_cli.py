from pathlib import Path

from typer.testing import CliRunner

from jwst_redux import cli
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import PipelineExecutionError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import DiscoveryResult, normalize_exposure


def _write_config(path: Path, output_root: Path, *, retention: str | None = None) -> None:
    options = "" if retention is None else f"options:\n  retention: {retention}\n"
    path.write_text(
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
output:
  root: {output_root}
{options}
""",
        encoding="utf-8",
    )


def test_search_and_plan_are_read_only(
    tmp_path, monkeypatch, exposure_records, product_records
) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records))
    products = select_starting_products(normalize_products(product_records), "uncal")
    exposures = attach_products(exposures, products)
    result = DiscoveryResult(datasets=build_science_datasets(exposures))
    monkeypatch.setattr(cli, "discover", lambda config: result)
    output_root = tmp_path / "must-not-exist"
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, output_root)
    runner = CliRunner()

    search = runner.invoke(cli.app, ["search", str(config_path)])
    assert search.exit_code == 0
    normalized_search = " ".join(search.stdout.split())
    assert "2 SOSS datasets containing 2 exposures and 6 _uncal products" in normalized_search
    assert "Dataset 1: GO-5799 Obs 001 / Visit 001" in normalized_search
    assert "Exposure 1: jw05799001001_04101_00001" in normalized_search
    assert "Segment 1: jw05799001001_04101_00001-seg001_nis_uncal.fits" in normalized_search
    assert "jw05799001001_04101_00001" in search.stdout
    assert "no files were downloaded or created" in search.stdout

    plan = runner.invoke(cli.app, ["plan", str(config_path)])
    assert plan.exit_code == 0
    normalized_plan = " ".join(plan.stdout.split())
    assert "2 SOSS datasets containing 2 exposures" in normalized_plan
    assert "2 compatible reduction branches" in normalized_plan
    assert "Dataset 1: GO-5799 Obs 001 / Visit 001" in normalized_plan
    assert "Branch 1: jw05799001001_04101_00001" in normalized_plan
    assert "Detector1Pipeline" in plan.stdout
    assert "Spec2Pipeline" in plan.stdout
    assert "Tso3Pipeline" in plan.stdout
    assert "Retention: all" in plan.stdout
    assert "no downloads, CRDS access, workspace creation" in normalized_plan
    assert not output_root.exists()


def test_plan_displays_configured_final_retention(
    tmp_path, monkeypatch, exposure_records, product_records
) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records))
    products = select_starting_products(normalize_products(product_records), "uncal")
    result = DiscoveryResult(datasets=build_science_datasets(attach_products(exposures, products)))
    monkeypatch.setattr(cli, "discover", lambda config: result)
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, tmp_path / "work", retention="final")

    plan = CliRunner().invoke(cli.app, ["plan", str(config_path)])

    assert plan.exit_code == 0
    assert "Retention: final" in plan.stdout


def test_format_bytes_keeps_small_products_visible() -> None:
    assert cli._format_bytes(1_100_160) == "1.10 MB (1.05 MiB)"


def test_run_reports_pipeline_error_to_stderr_without_masking_it(monkeypatch) -> None:
    monkeypatch.setattr(cli, "load_write_config", lambda _: object())

    def fail_pipeline(*_args, **_kwargs):
        raise PipelineExecutionError("original TSO3 pipeline error")

    monkeypatch.setattr(cli, "run_selected_through", fail_pipeline)
    result = CliRunner().invoke(cli.app, ["run", "ignored.yaml", "--through", "stage3"])

    assert result.exit_code == 1
    assert "original TSO3 pipeline error" in result.stderr
    assert "Console.print" not in result.output


def test_run_all_uses_batch_planned_endpoints_without_through(monkeypatch) -> None:
    prepared = object()
    batch_config = object()
    result = type("BatchResult", (), {"failures": ()})()
    calls: list[object] = []
    monkeypatch.setattr(cli, "load_batch_config", lambda _: batch_config)
    monkeypatch.setattr(cli, "prepare_batch", lambda _: prepared)
    monkeypatch.setattr(cli, "run_batch", lambda config, prepared: result)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda value: calls.append(value))
    monkeypatch.setattr(cli, "_print_batch_result", lambda value: calls.append(value))

    success = CliRunner().invoke(cli.app, ["run", "batch.yaml", "--all"])
    rejected = CliRunner().invoke(cli.app, ["run", "batch.yaml", "--all", "--through", "stage3"])

    assert success.exit_code == 0
    assert calls == [prepared, result]
    assert rejected.exit_code == 1
    assert "every branch uses its planned endpoint" in rejected.stderr
