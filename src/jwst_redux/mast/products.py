"""MAST product normalization and starting-product selection."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..exceptions import ArchiveQueryError, ConfigurationError
from ..models import Product

_SEGMENT_PATTERN = re.compile(r"-seg(?P<number>\d{3})_[^/]+_[^/]+\.fits$", re.IGNORECASE)
_NIRSPEC_DETECTOR_PATTERN = re.compile(
    r"(?:-seg\d{3})?_(?P<detector>nrs[12])_[^/]+\.fits$", re.IGNORECASE
)


def normalize_products(records: Iterable[dict[str, Any]]) -> tuple[Product, ...]:
    """Normalize JWST mission product rows."""
    products = []
    for record in records:
        filename = _required_text(record, "filename")
        products.append(
            Product(
                uri=_required_text(record, "uri"),
                filename=filename,
                calibration_level=_text(record.get("category")),
                product_type=_text(record.get("type")),
                size_bytes=_integer(record.get("size")),
                exposure_id=_text(record.get("dataset")),
                suffix=_lower_text(record.get("file_suffix")),
                access=_upper_text(record.get("access")),
                segment_number=segment_number(filename),
                detector=product_detector(record, filename),
                metadata=dict(record),
            )
        )
    return tuple(products)


def select_starting_products(products: Iterable[Product], start_from: str) -> tuple[Product, ...]:
    """Select unique science products for the configured starting level."""
    if start_from.lower() != "uncal":
        raise ConfigurationError("Only start_from='uncal' is implemented.")

    selected: dict[str, Product] = {}
    for product in products:
        if product.suffix != "_uncal":
            continue
        if (product.product_type or "").lower() != "science":
            continue
        selected.setdefault(product.uri, product)
    return tuple(sorted(selected.values(), key=_product_sort_key))


def segment_number(filename: str) -> int | None:
    """Return the three-digit JWST segment number when present."""
    match = _SEGMENT_PATTERN.search(filename)
    return int(match.group("number")) if match else None


def product_detector(record: dict[str, Any], filename: str) -> str | None:
    """Return normalized product-level detector metadata.

    The MAST product-list schema currently omits a detector column for JWST.
    STScI's detector-bearing exposure-product name is therefore normalized once
    at this archive boundary.  All downstream code uses ``Product.detector``.
    """
    explicit = _upper_text(record.get("detector"))
    if explicit is not None:
        return explicit
    match = _NIRSPEC_DETECTOR_PATTERN.search(filename)
    return match.group("detector").upper() if match else None


def _required_text(record: dict[str, Any], field: str) -> str:
    value = _text(record.get(field))
    if value is None:
        raise ArchiveQueryError(f"MAST product metadata is missing required field '{field}'.")
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _lower_text(value: Any) -> str | None:
    text = _text(value)
    return text.lower() if text is not None else None


def _upper_text(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text is not None else None


def _integer(value: Any) -> int | None:
    return None if value is None else int(value)


def _product_sort_key(product: Product) -> tuple[str, int, str]:
    segment = product.segment_number if product.segment_number is not None else -1
    return (product.exposure_id or "", segment, product.filename)
