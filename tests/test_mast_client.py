from pathlib import Path

from astropy.table import Table

from jwst_redux.mast.client import MastClient


class FakeBackend:
    def __init__(self) -> None:
        self.query_kwargs = None
        self.product_datasets = None
        self.download_args = None

    def query_criteria(self, **kwargs):
        self.query_kwargs = kwargs
        return Table({"fileSetName": ["dataset-1"], "program": [5799]})

    def get_product_list(self, datasets):
        self.product_datasets = datasets
        return Table({"filename": ["example_uncal.fits"], "size": [10]})

    def download_file(self, uri, *, local_path, cache, verbose):
        self.download_args = (uri, local_path, cache, verbose)
        return "COMPLETE", None, None


def test_client_converts_tables_and_only_lists_products() -> None:
    backend = FakeBackend()
    client = MastClient(backend)
    exposures = client.query_exposures({"targname": "TOI-3884"}, ("fileSetName", "program"))
    products = client.list_products(["dataset-1"])

    assert exposures == [{"fileSetName": "dataset-1", "program": 5799}]
    assert products == [{"filename": "example_uncal.fits", "size": 10}]
    assert backend.query_kwargs == {
        "targname": "TOI-3884",
        "select_cols": ["fileSetName", "program"],
    }
    assert backend.product_datasets == ["dataset-1"]


def test_client_downloads_via_mast_backend_to_exact_path(tmp_path: Path) -> None:
    backend = FakeBackend()
    client = MastClient(backend)
    destination = tmp_path / "raw" / "example.fits"

    result = client.download_product("mast:JWST/product/example.fits", destination)

    assert result == ("COMPLETE", None, None)
    assert backend.download_args == (
        "mast:JWST/product/example.fits",
        destination,
        False,
        True,
    )
