from jwst_redux.mast.products import normalize_products, segment_number, select_starting_products


def test_product_selection_filters_sorts_and_deduplicates(product_records) -> None:
    records = product_records[:4]
    records.append(dict(records[1]))
    products = select_starting_products(normalize_products(records), "uncal")
    assert [product.segment_number for product in products] == [1, 2, 3]
    assert all(product.suffix == "_uncal" for product in products)
    assert len(products) == 3
    assert all(product.exposure_id == "jw05799001001_04101_00001" for product in products)


def test_segment_number_handles_segmented_and_unsegmented_names() -> None:
    assert segment_number("jw00001-seg012_nis_uncal.fits") == 12
    assert segment_number("jw00001_nis_uncal.fits") is None
