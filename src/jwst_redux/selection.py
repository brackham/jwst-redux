"""Authoritative product selection through the scientific domain hierarchy."""

from __future__ import annotations

from dataclasses import replace
from typing import TypeVar

from .config import ExposureSelectionConfig
from .exceptions import SelectionError
from .models import ScienceDataset, SelectedExposure, SelectedProduct
from .modes import BOTS_MODE, mode_key, nirspec_bots_detectors
from .planning.associations import validate_exposure_segments

T = TypeVar("T")


def select_exposure(
    datasets: tuple[ScienceDataset, ...], selection: ExposureSelectionConfig
) -> SelectedExposure:
    """Select one configured exposure and validate its complete ordered segment set."""
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
        exposure for exposure in dataset.exposures if exposure.exposure_id == selection.exposure_id
    ]
    exposure = _exactly_one(
        matching_exposures,
        "exposure",
        {"exposure_id": selection.exposure_id, "dataset_id": dataset.dataset_id},
    )

    detector = selection.detector
    if mode_key(exposure) == BOTS_MODE:
        applicable = nirspec_bots_detectors(exposure)
        if detector is None:
            if len(applicable) != 1:
                raise SelectionError(
                    "NIRSpec/BOTS selection must specify detector when the configuration uses "
                    f"multiple science detectors: {', '.join(applicable)}."
                )
            detector = applicable[0]
        if detector not in applicable:
            raise SelectionError(
                f"Detector {detector} is not a science branch for "
                f"{exposure.grating}/{exposure.filter}; expected {', '.join(applicable)}."
            )

    products = tuple(
        sorted(
            (
                product
                for product in exposure.products
                if product.suffix == "_uncal"
                and (detector is None or (product.detector or exposure.detector) == detector)
            ),
            key=lambda product: (product.segment_number or 0, product.filename),
        )
    )
    if not products:
        detector_note = "" if detector is None else f" for detector {detector}"
        raise SelectionError(
            f"Selected exposure {exposure.exposure_id} has no _uncal products{detector_note}."
        )
    if detector is not None:
        products = tuple(
            product if product.detector is not None else replace(product, detector=detector)
            for product in products
        )
    validate_exposure_segments(exposure, products)
    selected_exposure = (
        replace(exposure, detector=detector, products=products)
        if detector is not None
        else exposure
    )
    return SelectedExposure(dataset=dataset, exposure=selected_exposure, products=products)


def select_stage1_product(
    datasets: tuple[ScienceDataset, ...], selection: ExposureSelectionConfig
) -> SelectedProduct:
    """Return the sole selected product for backwards-compatible single-product callers."""
    selected = select_exposure(datasets, selection)
    if len(selected.products) != 1:
        raise SelectionError(
            "The selected exposure has multiple segments; use selected-exposure execution instead."
        )
    return SelectedProduct(selected.dataset, selected.exposure, selected.products[0])


def _exactly_one(matches: list[T], noun: str, selector: dict) -> T:
    if len(matches) != 1:
        raise SelectionError(
            f"Expected exactly one {noun} for selector {selector!r}; found {len(matches)}."
        )
    return matches[0]
