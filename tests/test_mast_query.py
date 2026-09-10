from pathlib import Path

import pytest

from jwst_redux.config import DiscoveryConfig, QueryConfig
from jwst_redux.exceptions import ArchiveQueryError
from jwst_redux.mast.query import discover


class FakeArchiveClient:
    def __init__(self, exposures, products) -> None:
        self.exposures = exposures
        self.products = products
        self.criteria = None
        self.columns = None
        self.exposure_ids = None

    def query_exposures(self, criteria, select_columns):
        self.criteria = criteria
        self.columns = select_columns
        return self.exposures

    def list_products(self, exposure_ids):
        self.exposure_ids = exposure_ids
        return self.products


@pytest.fixture
def discovery_config() -> DiscoveryConfig:
    return DiscoveryConfig(
        query=QueryConfig(
            target="TOI-3884",
            target_match="mast_targname",
            archive_target_names=("TOI-3884",),
            instrument="NIRISS",
            exposure_type="NIS_SOSS",
        ),
        start_from="uncal",
        output_root=Path("work/toi3884"),
    )


def test_discover_queries_exact_soss_datasets_and_normalizes(
    discovery_config, exposure_records, product_records
) -> None:
    client = FakeArchiveClient(exposure_records, product_records)
    result = discover(discovery_config, client)

    assert client.criteria == {
        "targname": "TOI-3884",
        "instrume": "NIRISS",
        "exp_type": "NIS_SOSS",
        "productLevel": "1b",
    }
    assert client.exposure_ids == [
        "jw05799001001_04101_00001",
        "jw05799002001_04101_00001",
    ]
    assert len(result.datasets) == 2
    assert len(result.exposures) == 2
    assert result.exposures[0].program_id == "05799"
    assert result.exposures[0].observation_id == "001"
    assert result.exposures[0].is_tso is True
    assert result.exposures[0].optical_elements == "CLEAR;GR700XD"
    assert result.datasets[0].identity == (
        ("program_id", "05799"),
        ("observation_id", "001"),
        ("visit_number", "001"),
    )
    assert result.datasets[0].exposures == (result.exposures[0],)
    assert [product.segment_number for product in result.products[:3]] == [1, 2, 3]
    assert len(result.products) == 6


def test_discover_applies_optional_program_observation_and_visit_filters(
    exposure_records, product_records
) -> None:
    config = DiscoveryConfig(
        query=QueryConfig(
            target="TOI-3884",
            target_match="mast_targname",
            archive_target_names=("TOI-3884", "TOI-3884b"),
            instrument="NIRISS",
            exposure_type="NIS_SOSS",
            proposal_ids=("5799", "5863"),
            observation_id="1",
            visit_id="05799001001",
        ),
        start_from="uncal",
        output_root=Path("work/toi3884"),
    )
    client = FakeArchiveClient(exposure_records[:1], product_records[:4])
    discover(config, client)
    assert client.criteria["targname"] == "TOI-3884,TOI-3884b"
    assert client.criteria["program"] == "5799,5863"
    assert client.criteria["observtn"] == "1"
    assert client.criteria["visit_id"] == "05799001001"


def test_discover_fails_loudly_on_empty_result(discovery_config) -> None:
    with pytest.raises(ArchiveQueryError, match="no matching"):
        discover(discovery_config, FakeArchiveClient([], []))
