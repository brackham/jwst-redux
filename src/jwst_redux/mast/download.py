"""Idempotent single-product downloads through Astroquery/MAST."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..exceptions import DownloadError
from ..models import Product
from .client import MastClient


class ProductDownloader(Protocol):
    def download_product(
        self, uri: str, destination: Path
    ) -> tuple[str, str | None, str | None]: ...


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    reused: bool


def ensure_downloaded(
    product: Product,
    raw_dir: Path,
    *,
    overwrite: bool = False,
    downloader: ProductDownloader | None = None,
) -> DownloadResult:
    """Download one product atomically or reuse a size-validated local copy."""
    destination = raw_dir / product.filename
    if not overwrite and _is_complete(destination, product.size_bytes):
        return DownloadResult(path=destination, size_bytes=destination.stat().st_size, reused=True)

    partial = destination.with_name(f"{destination.name}.part")
    partial.unlink(missing_ok=True)
    archive = downloader or MastClient()
    status, message, _ = archive.download_product(product.uri, partial)
    if status != "COMPLETE":
        partial.unlink(missing_ok=True)
        raise DownloadError(
            f"MAST download failed for {product.filename}: {status}"
            + (f" ({message})" if message else "")
        )
    if not _is_complete(partial, product.size_bytes):
        actual = partial.stat().st_size if partial.exists() else 0
        partial.unlink(missing_ok=True)
        raise DownloadError(
            f"Downloaded size mismatch for {product.filename}: "
            f"expected {product.size_bytes}, received {actual}."
        )
    partial.replace(destination)
    return DownloadResult(path=destination, size_bytes=destination.stat().st_size, reused=False)


def _is_complete(path: Path, expected_size: int | None) -> bool:
    if not path.is_file():
        return False
    actual_size = path.stat().st_size
    if expected_size is None:
        return actual_size > 0
    return actual_size == expected_size
