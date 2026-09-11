"""Authoritative product selection through the scientific domain hierarchy."""

from __future__ import annotations

from typing import TypeVar

from .config import Stage1SelectionConfig
from .exceptions import SelectionError
from .models import ScienceDataset, SelectedProduct

T = TypeVar("T")


def select_stage1_product(
    datasets: tuple[ScienceDataset, ...], selection: Stage1SelectionConfig
) -> SelectedProduct:
    """Select exactly one configured product by traversing dataset/exposure/product children."""
    identity = {
        "program_id": selection.program_id,
        "observation_id": selection.observation_id,
        "visit_number": selection.visit_number,
    }
    matching_datasets = [
        dataset
        for dataset in datasets
        if all(dict(dataset.identity).get(field) == value for field, value in identity.items())
    ]
    dataset = _exactly_one(matching_datasets, "scientific dataset", identity)

    matching_exposures = [
        exposure
        for exposure in dataset.exposures
        if exposure.exposure_id == selection.exposure_id
    ]
    exposure = _exactly_one(
        matching_exposures,
        "exposure",
        {"exposure_id": selection.exposure_id, "dataset_id": dataset.dataset_id},
    )

    matching_products = [
        product
        for product in exposure.products
        if product.segment_number == selection.segment_number
        and product.filename == selection.filename
        and product.suffix == "_uncal"
    ]
    product = _exactly_one(
        matching_products,
        "_uncal product",
        {
            "filename": selection.filename,
            "segment_number": selection.segment_number,
            "exposure_id": exposure.exposure_id,
        },
    )
    return SelectedProduct(dataset=dataset, exposure=exposure, product=product)


def _exactly_one(matches: list[T], noun: str, selector: dict) -> T:
    if len(matches) != 1:
        raise SelectionError(
            f"Expected exactly one {noun} for selector {selector!r}; found {len(matches)}."
        )
    return matches[0]
