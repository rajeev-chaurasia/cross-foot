"""What a PDF is allowed to cost before it is rendered, and what a refusal buys.

Every fixture here is assembled in code rather than committed, because each one
is hostile by construction: a page declared the largest size the format allows,
a document of more pages than any statement has, bytes that stop mid file. The
first is the cheapest attack in the set. A 333 byte file declaring a 14400 by
14400 MediaBox renders to 1.3 gigapixels at vision dpi, which took this process
to 9.4 GB over 32 seconds before the budget existed, and the guard PIL is
assumed to provide never ran: pdfium's buffer reaches PIL through frombuffer,
and MAX_IMAGE_PIXELS is checked only for a file PIL itself decodes.

Speed is not the point of the refusal; being typed is. An unhandled raise
reaches the batch as a document with no result, which this codebase classifies
as transient, so the same hostile file would be rasterized again on every
resume. The last test here follows one all the way through `crossfoot extract`
to prove the run records it as finished.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pdf_fixtures import TRUTH_DOC, minimal_pdf, statement_items

from crossfoot import cli, pdfium
from crossfoot.api import crop_render
from crossfoot.api.dto import CropUnavailableReason
from crossfoot.constants import (
    MAX_PAGE_PIXELS,
    IngestErrorKind,
    LlmMode,
    QualityTier,
    SplitName,
)
from crossfoot.db.crops import CropSource
from crossfoot.extraction import llm_vision
from crossfoot.extraction.failures import FAILURE_CLASSES, FailureClass
from crossfoot.extraction.llm_vision import RasterizeError, rasterize_pdf

# The vision path reuses the born-digital reader's ceilings rather than keeping
# a second pair, so the assertions read them from where they are declared.
from crossfoot.extraction.pdf_text import MAX_FILE_BYTES, MAX_PAGES
from crossfoot.models.manifest import DatasetManifest, ManifestRecord

DOC_ID = "doc-hostile-01"
TEMPLATE_ID = "meridian-parts_statement-scan-v1"
# The largest MediaBox the format allows, 200 inches a side.
ABSURD_POINTS = 14_400
LETTER_POINTS = (612, 792)
# The measured refusal is hundredths of a second against 32 seconds of
# rendering, so this bound separates the two without pinning either.
IMMEDIATE_SECONDS = 10.0


def _assemble(objects: tuple[bytes, ...]) -> bytes:
    """Object bodies wrapped in a well-formed xref table and trailer."""
    buffer = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_offset = len(buffer)
    buffer += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        buffer += b"%010d 00000 n \n" % offset
    buffer += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_offset,
    )
    return bytes(buffer)


def _blank_pdf(width: int, height: int, *, pages: int = 1) -> bytes:
    """Pages carrying no content stream, so only their declared size costs anything."""
    first_page = 3
    kids = b" ".join(b"%d 0 R" % (first_page + index) for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
    ]
    objects.extend(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] >>" % (width, height)
        for _ in range(pages)
    )
    return _assemble(tuple(objects))


def _write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def _forbid_render(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any page reaches the encoder, which only a render can do."""

    def _forbidden(bitmap: object) -> bytes:
        raise AssertionError("an over-budget document must be refused before it renders")

    monkeypatch.setattr(llm_vision, "_png_bytes", _forbidden)


# ---------------------------------------------------------------------------
# The page budget
# ---------------------------------------------------------------------------


def test_an_absurd_mediabox_is_refused_before_a_bitmap_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_render(monkeypatch)
    path = _write(tmp_path / "absurd.pdf", _blank_pdf(ABSURD_POINTS, ABSURD_POINTS))
    assert path.stat().st_size < 1024  # the whole attack, in under a kilobyte

    with pytest.raises(RasterizeError) as raised:
        rasterize_pdf(path)

    assert raised.value.kind is IngestErrorKind.TOO_LARGE
    assert str(MAX_PAGE_PIXELS) in raised.value.detail


def test_the_absurd_mediabox_costs_nothing_to_refuse(tmp_path: Path) -> None:
    path = _write(tmp_path / "absurd.pdf", _blank_pdf(ABSURD_POINTS, ABSURD_POINTS))
    start = time.perf_counter()
    with pytest.raises(RasterizeError):
        rasterize_pdf(path)
    assert time.perf_counter() - start < IMMEDIATE_SECONDS


def test_the_budget_is_measured_at_the_dpi_the_page_is_rendered_at(tmp_path: Path) -> None:
    """A page inside the budget at 72 dpi can be outside it at vision dpi."""
    side = 2_400  # 5.76 megapixels of points, 36 megapixels at 180 dpi
    path = _write(tmp_path / "large.pdf", _blank_pdf(side, side))
    with pytest.raises(RasterizeError):
        rasterize_pdf(path)
    assert len(rasterize_pdf(path, dpi=36)) == 1


def test_a_statement_sized_page_still_rasterizes(tmp_path: Path) -> None:
    path = _write(tmp_path / "statement.pdf", minimal_pdf(statement_items(TRUTH_DOC)))
    pages = rasterize_pdf(path)
    assert [page.page for page in pages] == [0]
    assert pages[0].png_bytes.startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# The page count and file size ceilings
# ---------------------------------------------------------------------------


def test_a_document_over_the_page_ceiling_is_refused_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_render(monkeypatch)
    width, height = LETTER_POINTS
    path = _write(tmp_path / "many.pdf", _blank_pdf(width, height, pages=MAX_PAGES + 1))

    with pytest.raises(RasterizeError) as raised:
        rasterize_pdf(path)

    assert raised.value.kind is IngestErrorKind.TOO_LARGE
    assert str(MAX_PAGES) in raised.value.detail


def test_a_document_at_the_page_ceiling_is_served(tmp_path: Path) -> None:
    width, height = LETTER_POINTS
    path = _write(tmp_path / "fifty.pdf", _blank_pdf(width, height, pages=MAX_PAGES))
    assert len(rasterize_pdf(path)) == MAX_PAGES


def test_an_oversize_file_is_refused_before_pdfium_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "huge.pdf"
    with path.open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)

    def _forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("an oversize file must be refused before it is opened")

    monkeypatch.setattr(pdfium, "open_document", _forbidden)

    with pytest.raises(RasterizeError) as raised:
        rasterize_pdf(path)

    assert raised.value.kind is IngestErrorKind.TOO_LARGE
    assert str(MAX_FILE_BYTES) in raised.value.detail


# ---------------------------------------------------------------------------
# Everything else that can go wrong on the way to a bitmap
# ---------------------------------------------------------------------------


def test_damaged_bytes_become_a_typed_error_rather_than_a_raise(tmp_path: Path) -> None:
    whole = minimal_pdf(statement_items(TRUTH_DOC))
    path = _write(tmp_path / "truncated.pdf", whole[: len(whole) // 3])

    with pytest.raises(RasterizeError) as raised:
        rasterize_pdf(path)

    assert raised.value.kind is IngestErrorKind.TRUNCATED


def test_a_file_that_is_not_there_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(RasterizeError) as raised:
        rasterize_pdf(tmp_path / "absent.pdf")
    assert raised.value.kind is IngestErrorKind.UNRECOGNIZED


def test_every_kind_a_refusal_carries_is_a_permanent_failure() -> None:
    """A refusal the checkpoint reads as transient would be retried forever."""
    for kind in (
        IngestErrorKind.TOO_LARGE,
        IngestErrorKind.TRUNCATED,
        IngestErrorKind.UNRECOGNIZED,
    ):
        assert FAILURE_CLASSES[kind] is FailureClass.PERMANENT


# ---------------------------------------------------------------------------
# The review crop path, which rasterizes the same way at its own dpi
# ---------------------------------------------------------------------------


def test_a_review_crop_refuses_a_page_over_the_budget(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _write(dataset / "scan.pdf", _blank_pdf(ABSURD_POINTS, ABSURD_POINTS))
    source = CropSource(file_path="scan.pdf", page=0, bbox=None)

    with pytest.raises(crop_render.CropSourceError) as raised:
        crop_render.render_crop_file(
            source=source, dataset_dir=dataset, destination=tmp_path / "crop.png"
        )

    assert raised.value.reason is CropUnavailableReason.SOURCE_UNREADABLE
    assert str(MAX_PAGE_PIXELS) in raised.value.detail


def test_a_review_crop_of_a_statement_sized_page_is_still_served(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _write(dataset / "scan.pdf", minimal_pdf(statement_items(TRUTH_DOC)))
    destination = tmp_path / "crops" / "crop.png"

    crop_render.render_crop_file(
        source=CropSource(file_path="scan.pdf", page=0, bbox=None),
        dataset_dir=dataset,
        destination=destination,
    )

    assert destination.read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# The run: a document that cannot be rasterized has to be finished, not owed
# ---------------------------------------------------------------------------


def _dataset_with_hostile_scan(root: Path) -> Path:
    dataset = root / "dataset"
    (dataset / "files").mkdir(parents=True)
    _write(dataset / "files" / "hostile.pdf", _blank_pdf(ABSURD_POINTS, ABSURD_POINTS))
    manifest = DatasetManifest(
        master_seed=1,
        generator_version="test",
        config_hash="hostile",
        records=(
            ManifestRecord(
                doc_id=DOC_ID,
                file_path="files/hostile.pdf",
                quality_tier=QualityTier.SCAN_HEAVY,
                template_id=TEMPLATE_ID,
                render_seed=1,
                truth=TRUTH_DOC,
                split=SplitName.TEST,
            ),
        ),
    )
    (dataset / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    return dataset


async def test_a_document_that_cannot_be_rasterized_is_finished_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = _dataset_with_hostile_scan(tmp_path)
    monkeypatch.setattr(cli, "EXTRACTIONS_DIR", tmp_path / "extractions")
    monkeypatch.setattr(cli, "COST_DB", tmp_path / "costs.db")
    monkeypatch.setattr(cli, "RUN_STATE_DB", tmp_path / "runstate.db")
    monkeypatch.setattr(cli, "RESPONSE_CACHE_DB", tmp_path / "llm_cache.db")

    counts = await cli._extract_split(dataset, SplitName.TEST, LlmMode.REPLAY, False, 1)

    # Finished, so a resume owes it nothing. Retried forever is what pending
    # would mean, and no provider was ever asked for it.
    assert (counts.extracted, counts.unprocessable, counts.pending_retry) == (0, 1, 0)
    written = next((tmp_path / "extractions").glob("*.json")).read_text(encoding="utf-8")
    assert IngestErrorKind.TOO_LARGE.value in written
    assert str(MAX_PAGE_PIXELS) in written
