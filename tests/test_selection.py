from dataclasses import replace
from pathlib import Path

import pytest

from jwst_redux.config import load_write_config
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import SelectionError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import normalize_exposure
from jwst_redux.selection import select_stage1_product


def test_selection_traverses_dataset_exposure_product_hierarchy(
    exposure_records, product_records
) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records))
    products = select_starting_products(normalize_products(product_records), "uncal")
    datasets = build_science_datasets(attach_products(exposures, products))
    config = load_write_config(Path(__file__).parents[1] / "configs" / "toi3884.yaml")

    selected = select_stage1_product(datasets, config.selection)

    assert dict(selected.dataset.identity) == {
        "program_id": "05799",
        "observation_id": "001",
        "visit_number": "001",
    }
    assert selected.exposure.exposure_id == "jw05799001001_04101_00001"
    assert selected.product.segment_number == 1
    assert selected.product.filename == "jw05799001001_04101_00001-seg001_nis_uncal.fits"

    missing = replace(config.selection, segment_number=2, filename="wrong.fits")
    with pytest.raises(SelectionError, match="found 0"):
        select_stage1_product(datasets, missing)
