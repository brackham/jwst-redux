from dataclasses import replace
from pathlib import Path

import pytest

from jwst_redux.config import DiscoveryConfig, QueryConfig
from jwst_redux.datasets import attach_products, build_science_datasets
from jwst_redux.exceptions import PlanningError
from jwst_redux.mast.products import normalize_products, select_starting_products
from jwst_redux.mast.query import normalize_exposure
from jwst_redux.planning.associations import build_compatible_groups
from jwst_redux.planning.resolver import make_reduction_plans


def _config() -> DiscoveryConfig:
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


def test_compatible_exposures_can_share_a_reduction_group(
    exposure_records, product_records
) -> None:
    first = normalize_exposure(exposure_records[0])
    second_record = {
        **exposure_records[0],
        "ArchiveFileID": 999,
        "fileSetName": "same-observation-second-exposure",
    }
    second = normalize_exposure(second_record)
    first_products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    second_products = tuple(
        replace(
            product,
            exposure_id="same-observation-second-exposure",
            uri=f"second/{product.uri}",
        )
        for product in first_products
    )
    exposures = attach_products((first, second), first_products + second_products)
    dataset = build_science_datasets(exposures)[0]

    groups = build_compatible_groups(dataset)
    assert len(groups) == 1
    assert {exposure.exposure_id for exposure in groups[0].exposures} == {
        "jw05799001001_04101_00001",
        "same-observation-second-exposure",
    }


def test_toi3884_exposures_form_two_dataset_plans(exposure_records, product_records) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records))
    products = select_starting_products(normalize_products(product_records), "uncal")
    datasets = build_science_datasets(attach_products(exposures, products))
    plans = make_reduction_plans(_config(), datasets)
    assert len(plans) == 2
    assert all(len(plan.reductions) == 1 for plan in plans)
    assert all(
        [product.segment_number for product in plan.reductions[0].group.products] == [1, 2, 3]
        for plan in plans
    )


def test_missing_segment_is_rejected(exposure_records, product_records) -> None:
    exposure = normalize_exposure(exposure_records[0])
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    dataset = build_science_datasets(attach_products((exposure,), products[:-1]))[0]
    with pytest.raises(PlanningError, match=r"segments \[1, 2\].*expected \[1, 2, 3\]"):
        build_compatible_groups(dataset)


def test_duplicate_segment_is_rejected(exposure_records, product_records) -> None:
    exposure = normalize_exposure(exposure_records[0])
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    duplicate = replace(products[0], uri="duplicate")
    dataset = build_science_datasets(attach_products((exposure,), products + (duplicate,)))[0]
    with pytest.raises(PlanningError, match="duplicate segment"):
        build_compatible_groups(dataset)


def test_resolver_builds_official_tso_path(exposure_records, product_records) -> None:
    exposures = tuple(map(normalize_exposure, exposure_records[:1]))
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    datasets = build_science_datasets(attach_products(exposures, products))
    plan = make_reduction_plans(_config(), datasets)[0].reductions[0]
    assert [stage.name for stage in plan.stages] == [
        "Detector1Pipeline",
        "Spec2Pipeline",
        "Tso3Pipeline",
    ]
    assert plan.stages[0].inputs[0].name.endswith("seg001_nis_uncal.fits")
    assert plan.stages[1].inputs[0].name.endswith("seg001_nis_rateints.fits")
    assert plan.stages[2].inputs[0].name.endswith("seg001_nis_calints.fits")
    assert plan.stages[2].association_required is True


def test_resolver_rejects_non_tso_soss(exposure_records, product_records) -> None:
    exposure = replace(normalize_exposure(exposure_records[0]), is_tso=False)
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    dataset = build_science_datasets(attach_products((exposure,), products))[0]
    with pytest.raises(PlanningError, match="TSOVISIT=true"):
        make_reduction_plans(_config(), (dataset,))


def test_single_integration_soss_stops_after_spec2(exposure_records, product_records) -> None:
    exposure = replace(
        normalize_exposure(exposure_records[0]),
        integration_count=1,
        segment_count=1,
    )
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    product = next(product for product in products if product.segment_number == 1)
    dataset = build_science_datasets(attach_products((exposure,), (product,)))[0]
    plan = make_reduction_plans(_config(), (dataset,))[0].reductions[0]
    assert [stage.name for stage in plan.stages] == ["Detector1Pipeline", "Spec2Pipeline"]
    assert "excluded from official Tso3" in plan.notes[0]


def test_f277w_soss_stops_after_detector1(exposure_records, product_records) -> None:
    exposure = replace(
        normalize_exposure(exposure_records[0]),
        optical_elements="F277W;GR700XD",
        segment_count=1,
    )
    products = select_starting_products(normalize_products(product_records[:4]), "uncal")
    product = next(product for product in products if product.segment_number == 1)
    dataset = build_science_datasets(attach_products((exposure,), (product,)))[0]
    plan = make_reduction_plans(_config(), (dataset,))[0].reductions[0]
    assert [stage.name for stage in plan.stages] == ["Detector1Pipeline"]
    assert "no supported pipeline spectral extraction" in plan.notes[0]
