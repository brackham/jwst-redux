from pathlib import Path

import pytest

from jwst_redux.config import load_write_config
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import SelectionError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import normalize_exposure
from jwst_redux.selection import select_exposure


def test_selection_traverses_dataset_exposure_and_all_segment_children(
    exposure_records, product_records
) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records))
    products = select_starting_products(normalize_products(product_records), "uncal")
    datasets = build_science_datasets(attach_products(exposures, products))
    config = load_write_config(Path(__file__).parents[1] / "configs" / "toi3884.yaml")

    selected = select_exposure(datasets, config.selection)

    assert dict(selected.dataset.identity) == {
        "program_id": "05799",
        "observation_id": "002",
        "visit_number": "001",
    }
    assert selected.exposure.exposure_id == "jw05799002001_04101_00001"
    assert [product.segment_number for product in selected.products] == [1, 2, 3]

    missing = type(config.selection)(
        program_id=config.selection.program_id,
        observation_id=config.selection.observation_id,
        visit_number=config.selection.visit_number,
        exposure_id="wrong-exposure",
    )
    with pytest.raises(SelectionError, match="found 0"):
        select_exposure(datasets, missing)
