"""MAST authentication and credential non-disclosure at persistence boundaries."""

import logging
import traceback
from pathlib import Path

import pytest
from astropy.table import Table

from jwst_redux.config import load_config, load_write_config
from jwst_redux.exceptions import ArchiveQueryError, ConfigurationError, DownloadError
from jwst_redux.mast.client import MastClient
from jwst_redux.mast.download import ensure_downloaded
from jwst_redux.models import Product
from jwst_redux.pipeline.runner import run_spec3
from jwst_redux.provenance import ManifestStore


class Backend:
    def __init__(self, *, fail=None):
        self.fail = fail
        self.logins = []
        self.verbose = None

    def login(self, *, token, store_token):
        self.logins.append((token, store_token))
        print(token)
        logging.getLogger("astroquery").critical(token)
        if self.fail == "login":
            raise RuntimeError(token)

    def query_criteria(self, **kwargs):
        if self.fail == "query":
            raise RuntimeError("backend echoed " + __import__("os").environ["MAST_API_TOKEN"])
        return Table({"fileSetName": ["public-or-proprietary"]})

    def get_product_list(self, datasets):
        return Table({"filename": ["test_uncal.fits"]})

    def download_file(self, uri, **kwargs):
        self.verbose = kwargs["verbose"]
        return "ERROR", __import__("os").environ.get("MAST_API_TOKEN"), None


def test_public_access_does_not_login_or_prompt(monkeypatch):
    monkeypatch.delenv("MAST_API_TOKEN", raising=False)
    backend = Backend()
    client = MastClient(backend)
    assert client.query_exposures({}, ("fileSetName",))
    assert backend.logins == []


def test_token_login_applies_to_queries_products_and_download_without_disclosure(
    monkeypatch, capsys, caplog, tmp_path
):
    token = "sentinel-mast-credential-keep-private"
    monkeypatch.setenv("MAST_API_TOKEN", token)
    backend = Backend()
    client = MastClient(backend)
    assert backend.logins == [(token, False)]
    assert client.query_exposures({}, ("fileSetName",))
    assert client.list_products(["id"])
    _status, message, _url = client.download_product("mast:JWST/product/file", tmp_path / "file")
    assert message == "[REDACTED]" and backend.verbose is False
    assert token not in capsys.readouterr().out + caplog.text
    assert token not in repr(vars(client))


@pytest.mark.parametrize("failure", ["login", "query"])
def test_transport_exception_traceback_is_safe(monkeypatch, capsys, caplog, failure):
    token = "sentinel-private-token"
    monkeypatch.setenv("MAST_API_TOKEN", token)
    with pytest.raises(ArchiveQueryError) as error:
        client = MastClient(Backend(fail=failure))
        client.query_exposures({}, ("fileSetName",))
    text = "".join(traceback.format_exception(error.value))
    assert token not in text + capsys.readouterr().out + caplog.text


def test_download_status_error_and_injected_transport_error_are_sanitized(monkeypatch, tmp_path):
    token = "sentinel-secret-mast-token"
    monkeypatch.setenv("MAST_API_TOKEN", token)
    product = Product("mast:JWST/product/a", "a_uncal.fits", suffix="_uncal")
    for fail in (False, True):

        class Downloader:
            def download_product(self, uri, destination, should_fail=fail):
                if should_fail:
                    raise RuntimeError(token)
                return "ERROR", token, None

        with pytest.raises(DownloadError) as error:
            ensure_downloaded(product, tmp_path, downloader=Downloader())
        assert token not in "".join(traceback.format_exception(error.value))
    assert not list(tmp_path.glob("*.part"))


def test_manifest_and_pipeline_log_redact_active_credentials(monkeypatch, tmp_path, capsys):
    token = "sentinel-secret-mast-token"
    monkeypatch.setenv("MAST_API_TOKEN", token)
    store = ManifestStore(tmp_path / "manifest.json", "public-target")
    store.initialize()
    store.append({"run_id": "test", "error": token, "nested": {"value": token}})
    assert token not in store.path.read_text()
    association = tmp_path / "asn.json"
    association.write_text("{}")
    output = tmp_path / "outputs"
    output.mkdir()

    def pipeline(path, directory, overrides):
        print(token)
        (directory / "spectrum_x1d.fits").write_bytes(b"result")

    log = tmp_path / "pipeline.log"
    run_spec3(association, output, log, "jwst_test.pmap", {}, pipeline=pipeline)
    assert token not in log.read_text() + capsys.readouterr().out


def test_credentials_are_rejected_from_config_without_echoing_values(tmp_path):
    path = tmp_path / "unsafe.yaml"
    path.write_text("pipeline:\n  MAST_API_TOKEN: sentinel-secret-value\n")
    with pytest.raises(ConfigurationError) as error:
        load_config(path)
    assert "sentinel-secret-value" not in str(error.value)


def test_pipeline_class_overrides_and_legacy_compatibility(tmp_path):
    import yaml

    source = Path(__file__).parents[1] / "configs" / "toi3884.yaml"
    data = yaml.safe_load(source.read_text())
    legacy_path = tmp_path / "legacy.yaml"
    legacy_path.write_text(yaml.safe_dump(data))
    legacy = load_write_config(legacy_path)
    assert legacy.overrides_for("Spec2Pipeline") == legacy.spec2_parameter_overrides
    data["query"].update(instrument="NIRSPEC", exposure_type="NRS_FIXEDSLIT")
    data["pipeline"] = {
        "stages": "auto",
        "class_overrides": {"Spec3Pipeline": {"steps": {"extract_1d": {"save_results": True}}}},
    }
    path = tmp_path / "fixedslit.yaml"
    path.write_text(yaml.safe_dump(data))
    fixed = load_write_config(path)
    assert fixed.selection.detector is None
    assert (
        fixed.overrides_for("Spec3Pipeline") == data["pipeline"]["class_overrides"]["Spec3Pipeline"]
    )
    data["pipeline"]["class_overrides"]["Spec2Pipeline"] = {}
    data["pipeline"]["spec2_overrides"] = {}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigurationError, match="both legacy"):
        load_write_config(path)


def test_pipeline_exception_text_and_traceback_never_disclose_token(monkeypatch, tmp_path, capsys):
    token = "sentinel-private-pipeline-token"
    monkeypatch.setenv("MAST_API_TOKEN", token)
    association = tmp_path / "asn.json"
    association.write_text("{}")
    output = tmp_path / "outputs"
    output.mkdir()

    def pipeline(*_):
        raise RuntimeError("unexpected backend diagnostic " + token)

    from jwst_redux.exceptions import PipelineExecutionError

    with pytest.raises(PipelineExecutionError) as error:
        run_spec3(
            association, output, tmp_path / "pipeline.log", "jwst_test.pmap", {}, pipeline=pipeline
        )
    assert token not in "".join(traceback.format_exception(error.value))
    assert token not in (tmp_path / "pipeline.log").read_text() + capsys.readouterr().out


def test_preexisting_logging_handler_is_captured_and_restored(tmp_path, capsys):
    import sys

    logger = logging.getLogger("jwst_redux_test_capture")
    handler = logging.StreamHandler(sys.stderr)
    original = handler.stream
    logger.addHandler(handler)
    previous_propagation = logger.propagate
    logger.propagate = False
    association = tmp_path / "asn.json"
    association.write_text("{}")
    output = tmp_path / "outputs"
    output.mkdir()

    def pipeline(path, directory, overrides):
        logger.warning("official step warning via an existing logging handler")
        (directory / "spectrum_x1d.fits").write_bytes(b"result")

    try:
        for i in range(2):
            run_spec3(
                association,
                output,
                tmp_path / f"pipeline-{i}.log",
                "jwst_test.pmap",
                {},
                pipeline=pipeline,
            )
            assert "official step warning" in (tmp_path / f"pipeline-{i}.log").read_text()
            assert handler.stream is original
    finally:
        logger.removeHandler(handler)
        logger.propagate = previous_propagation
