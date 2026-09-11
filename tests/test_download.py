from pathlib import Path

from jwst_redux.mast.download import ensure_downloaded
from jwst_redux.models import Product


class FakeDownloader:
    def __init__(self, content: bytes = b"test") -> None:
        self.content = content
        self.calls: list[tuple[str, Path]] = []

    def download_product(self, uri: str, destination: Path):
        self.calls.append((uri, destination))
        destination.write_bytes(self.content)
        return "COMPLETE", None, None


def _product() -> Product:
    return Product(
        uri="mast:JWST/product/example_uncal.fits",
        filename="example_uncal.fits",
        size_bytes=4,
        exposure_id="jw-example",
        suffix="_uncal",
        segment_number=1,
    )


def test_download_uses_raw_destination_and_reuses_complete_file(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    downloader = FakeDownloader()

    first = ensure_downloaded(_product(), raw, downloader=downloader)
    second = ensure_downloaded(_product(), raw, downloader=downloader)

    assert first.path == raw / "example_uncal.fits"
    assert first.path.read_bytes() == b"test"
    assert first.reused is False
    assert second.reused is True
    assert downloader.calls == [
        (
            "mast:JWST/product/example_uncal.fits",
            raw / "example_uncal.fits.part",
        )
    ]


def test_download_overwrite_replaces_existing_file(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    destination = raw / "example_uncal.fits"
    destination.write_bytes(b"old!")
    downloader = FakeDownloader(content=b"new!")

    result = ensure_downloaded(_product(), raw, overwrite=True, downloader=downloader)

    assert result.reused is False
    assert destination.read_bytes() == b"new!"
    assert len(downloader.calls) == 1


def test_download_replaces_obviously_incomplete_existing_file(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    destination = raw / "example_uncal.fits"
    destination.write_bytes(b"x")
    downloader = FakeDownloader()

    result = ensure_downloaded(_product(), raw, downloader=downloader)

    assert result.reused is False
    assert destination.read_bytes() == b"test"
    assert len(downloader.calls) == 1
