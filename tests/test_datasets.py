from dataclasses import replace
from pathlib import Path

import pytest

from jwst_redux.config import DiscoveryConfig, QueryConfig
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import ArchiveQueryError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import normalize_exposure
from jwst_redux.planning.resolver import make_reduction_plans


def _go5863_records(template: dict, observation: int) -> list[dict]:
    visit_id = f"05863{observation:03d}001"
    definitions = (
        ("04101", "CLEAR;GR700XD", "CLEAR", 1, 1),
        ("04102", "CLEAR;GR700XD", "CLEAR", 251, 2),
        ("04103", "F277W;GR700XD", "F277W", 10, 1),
    )
    return [
        {
            **template,
            "program": 5863,
            "observtn": observation,
            "visit": 1,
            "visit_id": visit_id,
            "targprop": "TOI-3884b",
            "targname": "TOI-3884b",
            "fileSetName": f"jw{visit_id}_{exposure}_00001",
            "ArchiveFileID": 586300000 + observation * 100 + index,
            "opticalElements": optical_elements,
            "filter": filter_name,
            "nints": integrations,
            "exsegtot": segments,
        }
        for index, (exposure, optical_elements, filter_name, integrations, segments) in enumerate(
            definitions, start=1
        )
    ]


def _uncal_records(exposure_records: list[dict]) -> list[dict]:
    records = []
    for exposure in exposure_records:
        exposure_id = exposure["fileSetName"]
        for segment in range(1, exposure["exsegtot"] + 1):
            filename = f"{exposure_id}-seg{segment:03d}_nis_uncal.fits"
            records.append(
                {
                    "access": "PUBLIC",
                    "dataset": exposure_id,
                    "filename": filename,
                    "uri": f"{exposure_id}/{filename}",
                    "file_suffix": "_uncal",
                    "category": "1b",
                    "size": 100,
                    "type": "science",
                }
            )
    return records


def _config() -> DiscoveryConfig:
    return DiscoveryConfig(
        query=QueryConfig(
            target="TOI-3884",
            target_match="mast_targname",
            archive_target_names=("TOI-3884", "TOI-3884b"),
            instrument="NIRISS",
            exposure_type="NIS_SOSS",
        ),
        start_from="uncal",
        output_root=Path("work/toi3884"),
    )


def test_toi3884_dataset_exposure_product_hierarchy(exposure_records) -> None:
    records = [
        *exposure_records,
        *_go5863_records(exposure_records[0], 1),
        *_go5863_records(exposure_records[0], 3),
    ]
    exposures = tuple(map(normalize_exposure, records))
    products = select_starting_products(normalize_products(_uncal_records(records)), "uncal")
    exposures = attach_products(exposures, products)
    datasets = build_science_datasets(exposures)

    assert len(datasets) == 4
    assert len(exposures) == 8
    assert len(products) == 14
    assert [len(dataset.exposures) for dataset in datasets] == [1, 1, 3, 3]
    assert datasets[2].identity == (
        ("program_id", "05863"),
        ("observation_id", "001"),
        ("visit_number", "001"),
    )
    assert [exposure.exposure_id.split("_")[1] for exposure in datasets[2].exposures] == [
        "04101",
        "04102",
        "04103",
    ]
    for exposure in datasets[2].exposures:
        assert len(exposure.products) == exposure.segment_count
        assert [product.segment_number for product in exposure.products] == list(
            range(1, exposure.segment_count + 1)
        )


def test_dataset_identity_is_mode_aware_and_not_blindly_visit_id(exposure_records) -> None:
    first = normalize_exposure(exposure_records[0])
    second = replace(
        first,
        exposure_id="same-science-dataset-second-exposure",
        visit_id="a-different-archive-value",
    )
    assert len(build_science_datasets((first, second))) == 1

    with pytest.raises(ArchiveQueryError, match="No scientific dataset identity rule"):
        build_science_datasets((replace(first, instrument="NIRCAM"),))


def test_toi3884_dataset_plans_preserve_exposure_specific_paths(exposure_records) -> None:
    records = [
        *exposure_records,
        *_go5863_records(exposure_records[0], 1),
        *_go5863_records(exposure_records[0], 3),
    ]
    exposures = tuple(map(normalize_exposure, records))
    products = select_starting_products(normalize_products(_uncal_records(records)), "uncal")
    exposures = attach_products(exposures, products)
    datasets = build_science_datasets(exposures)

    plans = make_reduction_plans(_config(), datasets)

    assert [len(plan.reductions) for plan in plans] == [1, 1, 3, 3]
    assert [
        tuple(stage.name for stage in reduction.stages)
        for reduction in plans[2].reductions
    ] == [
        ("Detector1Pipeline", "Spec2Pipeline"),
        ("Detector1Pipeline", "Spec2Pipeline", "Tso3Pipeline"),
        ("Detector1Pipeline",),
    ]
    assert all(
        reduction.group.dataset_id == dataset_plan.dataset.dataset_id
        for dataset_plan in plans
        for reduction in dataset_plan.reductions
    )
