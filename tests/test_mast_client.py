from astropy.table import Table

from jwst_redux.mast.client import MastClient


class FakeBackend:
    def __init__(self) -> None:
        self.query_kwargs = None
        self.product_datasets = None

    def query_criteria(self, **kwargs):
        self.query_kwargs = kwargs
        return Table({"fileSetName": ["dataset-1"], "program": [5799]})

    def get_product_list(self, datasets):
        self.product_datasets = datasets
        return Table({"filename": ["example_uncal.fits"], "size": [10]})


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
