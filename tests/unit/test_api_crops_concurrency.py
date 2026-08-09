"""Concurrent crop requests, which used to take the whole server down.

PDFium is not thread safe and FastAPI runs sync `def` handlers in a threadpool,
so the review queue asking for one crop per field put several threads inside the
library at once. Six concurrent workers raised an illegal instruction out of the
render call (WinError 0xc000001d), after which every later request failed and
the process exited. Nothing here needs the real crash: what is asserted is the
property that makes it impossible, that no two threads are ever inside a PDFium
document scope at the same time.

A serialization test can pass for the wrong reason, by never having had two
requests in flight to begin with, so the first test in this file proves the
opposite about the unlocked part of the same handler: eight requests reach a
barrier together after their renders are done. The lock is the only thing making
the render itself one at a time.

Offline throughout: the source document is the hand-assembled PDF fixture.
"""

import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pypdfium2
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pdf_fixtures import TRUTH_DOC, minimal_pdf, statement_items

from crossfoot import pdfium
from crossfoot.api import create_app
from crossfoot.constants import (
    CropKind,
    ExtractionRoute,
    FieldFamily,
    FieldName,
    FieldSource,
    QualityTier,
    ReviewStatus,
)
from crossfoot.db import connect
from crossfoot.db.schema import ensure_schema
from crossfoot.extraction import crops
from crossfoot.models.extraction import FieldSignals

# Eight threads against sixteen fields: enough that a request finds both a field
# no one else wants and a field three other threads are asking for at the same
# moment, which are the two shapes the queue produces.
THREADS = 8
DOCS = 4
FIELDS_PER_DOC = 4

# The first field of every document carries coordinates, so its crop is a box
# cut from the page rather than the page, and byte equality across threads is
# being claimed about more than one picture.
EXACT_FIELD_INDEX = 0
AMOUNT_BBOX = (0.78, 0.25, 0.92, 0.28)

# A document whose bytes are not a PDF at all, so opening it fails inside the
# lock and the handler leaves through the error path.
BROKEN_DOC = "doc-broken"
BROKEN_FIELD = "fld-broken-0001-line_amount"
CROP_UNAVAILABLE_STATUS = 424

# Long enough that eight renders of the fixture page finish first, short enough
# that a barrier which will never fill fails the test instead of hanging it.
BARRIER_TIMEOUT_SECONDS = 60.0

RENDER_FAILURE_DETAIL = "render blew up inside the lock"
# The library the lock stands in front of, spelled once for the guard test.
PDFIUM_LIBRARY = "pypdfium2"
_REAL_PDF_DOCUMENT = pypdfium2.PdfDocument

_INSERT_DOCUMENT = """
INSERT INTO documents (doc_id, file_path, doc_type, quality_tier, route, split, error_kind)
VALUES (:doc_id, :file_path, NULL, :quality_tier, 'digital_pdf', NULL, NULL)
"""

_INSERT_FIELD = """
INSERT INTO fields (
    field_id, doc_id, line_no, name, family, raw_text, value, value_cents, value_date,
    source, crop_kind, page, x0, y0, x1, y1, confidence, status, signals
) VALUES (
    :field_id, :doc_id, 1, :name, :family, '$1,234.56', '1234.56', 123456, NULL,
    :source, :crop_kind, :page, :x0, :y0, :x1, :y1, 0.2, :status, :signals
)
"""


def _doc_id(index: int) -> str:
    return f"doc-{index}"


def _field_id(doc_index: int, field_index: int) -> str:
    return f"fld-{doc_index}-{field_index:04d}-line_amount"


def _seed(connection: sqlite3.Connection) -> None:
    """Documents of several fields each, plus one document that cannot be read."""
    signals = FieldSignals(route=ExtractionRoute.DIGITAL_PDF).model_dump_json()
    for doc_index in range(DOCS):
        doc_id = _doc_id(doc_index)
        connection.execute(
            _INSERT_DOCUMENT,
            {
                "doc_id": doc_id,
                "file_path": f"files/{doc_id}.pdf",
                "quality_tier": QualityTier.CLEAN_DIGITAL.value,
            },
        )
        for field_index in range(FIELDS_PER_DOC):
            exact = field_index == EXACT_FIELD_INDEX
            x0, y0, x1, y1 = AMOUNT_BBOX
            connection.execute(
                _INSERT_FIELD,
                {
                    "field_id": _field_id(doc_index, field_index),
                    "doc_id": doc_id,
                    "name": FieldName.LINE_AMOUNT.value,
                    "family": FieldFamily.AMOUNT.value,
                    "source": FieldSource.DETERMINISTIC.value,
                    "crop_kind": (CropKind.EXACT_BBOX if exact else CropKind.FULL_PAGE).value,
                    "page": 0 if exact else None,
                    "x0": x0 if exact else None,
                    "y0": y0 if exact else None,
                    "x1": x1 if exact else None,
                    "y1": y1 if exact else None,
                    "status": ReviewStatus.NEEDS_REVIEW.value,
                    "signals": signals,
                },
            )
    connection.execute(
        _INSERT_DOCUMENT,
        {
            "doc_id": BROKEN_DOC,
            "file_path": f"files/{BROKEN_DOC}.pdf",
            "quality_tier": QualityTier.CLEAN_DIGITAL.value,
        },
    )
    connection.execute(
        _INSERT_FIELD,
        {
            "field_id": BROKEN_FIELD,
            "doc_id": BROKEN_DOC,
            "name": FieldName.LINE_AMOUNT.value,
            "family": FieldFamily.AMOUNT.value,
            "source": FieldSource.DETERMINISTIC.value,
            "crop_kind": CropKind.FULL_PAGE.value,
            "page": None,
            "x0": None,
            "y0": None,
            "x1": None,
            "y1": None,
            "status": ReviewStatus.NEEDS_REVIEW.value,
            "signals": signals,
        },
    )


@pytest.fixture
def crops_root(tmp_path: Path) -> Path:
    root = tmp_path / "crops"
    root.mkdir()
    return root


@pytest.fixture
def client(tmp_path: Path, crops_root: Path) -> Iterator[TestClient]:
    dataset = tmp_path / "dataset"
    files = dataset / "files"
    files.mkdir(parents=True)
    page = minimal_pdf(statement_items(TRUTH_DOC))
    for doc_index in range(DOCS):
        (files / f"{_doc_id(doc_index)}.pdf").write_bytes(page)
    (files / f"{BROKEN_DOC}.pdf").write_bytes(b"not a pdf, not even close\n" * 64)
    db_path = tmp_path / "crossfoot.db"
    with closing(connect(db_path)) as connection, connection:
        ensure_schema(connection)
        _seed(connection)
    scorecards_dir = tmp_path / "scorecards"
    scorecards_dir.mkdir()
    app = create_app(
        db_path=db_path,
        crops_root=crops_root,
        scorecards_dir=scorecards_dir,
        dataset_dir=dataset,
    )
    # Server side failures come back as a status rather than being re-raised in
    # the calling thread, so a handler that dies inside the lock can be observed
    # the way a browser observes it.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _url(doc_id: str, field_id: str) -> str:
    return f"/api/crops/{doc_id}/{field_id}.png"


def _hammer(client: TestClient, urls: list[str]) -> list[Response]:
    """Every url fetched at once, by THREADS workers sharing the queue."""
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        return list(pool.map(client.get, urls))


def _queue_urls() -> list[str]:
    """One url per field, then the first document's fields again.

    The repeats are what a reviewer moving back through the queue produces, and
    they are the case where two threads race to render the same file.
    """
    urls = [
        _url(_doc_id(doc_index), _field_id(doc_index, field_index))
        for doc_index in range(DOCS)
        for field_index in range(FIELDS_PER_DOC)
    ]
    return urls + urls[:THREADS]


@dataclass
class Gate:
    """A counter of how many threads are inside a region at once."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    inside: int = 0
    most: int = 0

    @contextmanager
    def entered(self) -> Iterator[None]:
        with self.lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
        try:
            yield
        finally:
            with self.lock:
                self.inside -= 1


def test_the_handler_really_runs_eight_requests_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test below: without the lock, renders would overlap.

    The region instrumented here is the part of the handler after the page has
    been rasterized, which the lock does not cover. Every thread waits there for
    the other seven, so the barrier only clears if eight requests are genuinely
    in flight through the threadpool, and the whole test hangs to a timeout and
    fails if they are not.
    """
    barrier = threading.Barrier(THREADS)
    real_region = crops.review_region

    def waiting_region(*args: Any, **kwargs: Any) -> crops.Region:
        barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        region: crops.Region = real_region(*args, **kwargs)
        return region

    monkeypatch.setattr(crops, "review_region", waiting_region)
    urls = [
        _url(_doc_id(index % DOCS), _field_id(index % DOCS, index // DOCS))
        for index in range(THREADS)
    ]
    responses = _hammer(client, urls)
    assert [response.status_code for response in responses] == [200] * THREADS


def test_the_render_never_runs_two_at_a_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: at most one thread inside a PDFium document scope."""
    gate = Gate()
    real_open = pdfium.open_document

    @contextmanager
    def counted_open(path: Path) -> Iterator[Any]:
        with real_open(path) as document, gate.entered():
            yield document

    monkeypatch.setattr(pdfium, "open_document", counted_open)
    responses = _hammer(client, _queue_urls())

    assert [response.status_code for response in responses] == [200] * len(responses)
    # Counted inside the lock, so anything above one is two threads in PDFium.
    assert gate.most == 1
    assert gate.inside == 0


def test_concurrent_requests_for_one_field_agree_byte_for_byte(client: TestClient) -> None:
    """Every response is a PNG, and one field is one picture however it was raced for."""
    urls = _queue_urls()
    responses = _hammer(client, urls)

    by_url: dict[str, set[bytes]] = {}
    for url, response in zip(urls, responses, strict=True):
        assert response.status_code == 200, url
        assert response.headers["content-type"].startswith("image/png")
        by_url.setdefault(url, set()).add(response.content)
    repeated = [url for url in by_url if urls.count(url) > 1]
    assert len(repeated) >= THREADS
    for url in by_url:
        assert len(by_url[url]) == 1, f"{url} served more than one image"

    # The pictures are not all the same picture, so byte equality above is a
    # claim about rendering and not about a single cached page.
    assert len({next(iter(images)) for images in by_url.values()}) > 1


def test_concurrent_rendering_leaves_one_finished_file_per_field(
    client: TestClient, crops_root: Path
) -> None:
    """The cache holds what was served, and no half-written file survives."""
    responses = _hammer(client, _queue_urls())
    assert {response.status_code for response in responses} == {200}

    written = sorted(path for path in crops_root.rglob("*") if path.is_file())
    assert len(written) == DOCS * FIELDS_PER_DOC
    assert not list(crops_root.rglob("*.pending"))
    for path in written:
        # Decoded rather than trusted: a truncated PNG is still a file.
        assert crops.decode_png(path.read_bytes()).size > 0


def test_a_document_that_cannot_be_opened_leaves_the_lock_free(client: TestClient) -> None:
    """The real failure path: PDFium raises inside the lock, and the next crop still renders."""
    broken = client.get(_url(BROKEN_DOC, BROKEN_FIELD))
    assert broken.status_code == CROP_UNAVAILABLE_STATUS

    good = client.get(_url(_doc_id(0), _field_id(0, 0)))
    assert good.status_code == 200


class _ExplodingPage:
    """A page that fails to rasterize, the way a real one did on the sixth worker."""

    def render(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(RENDER_FAILURE_DETAIL)


class _ExplodingDocument:
    """A real document that opens and closes, wrapping pages that will not render."""

    def __init__(self, path: Path) -> None:
        self.document = _REAL_PDF_DOCUMENT(path)
        self.closed = False

    def __len__(self) -> int:
        return len(self.document)

    def __getitem__(self, index: int) -> _ExplodingPage:
        return _ExplodingPage()

    def close(self) -> None:
        self.closed = True
        self.document.close()


def test_an_exception_inside_the_render_leaves_the_lock_free(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure the route does not expect still releases the lock on its way out.

    The request itself is answered with a 500, which is correct: a render that
    raises something other than a PDFium error is a bug and not a fact about the
    document. What would end the demo is the next request blocking forever on a
    lock the failed one never gave back, so that is what is checked after it.
    """
    monkeypatch.setattr(pypdfium2, "PdfDocument", _ExplodingDocument)
    failed = client.get(_url(_doc_id(0), _field_id(0, 0)))
    assert failed.status_code == 500

    monkeypatch.undo()
    assert not pdfium._PDFIUM_LOCK.locked()
    responses = _hammer(client, _queue_urls())
    assert {response.status_code for response in responses} == {200}


def test_the_lock_and_the_document_are_released_when_the_body_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context manager's own contract, without a server in the way."""
    path = tmp_path / "page.pdf"
    path.write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
    opened: list[_ExplodingDocument] = []

    def recording_document(source: Path) -> _ExplodingDocument:
        document = _ExplodingDocument(source)
        opened.append(document)
        return document

    monkeypatch.setattr(pypdfium2, "PdfDocument", recording_document)
    with (
        pytest.raises(RuntimeError, match=RENDER_FAILURE_DETAIL),
        pdfium.open_document(path) as document,
    ):
        document[0].render()

    monkeypatch.undo()
    assert [document.closed for document in opened] == [True]
    assert not pdfium._PDFIUM_LOCK.locked()
    with pdfium.open_document(path) as reopened:
        assert len(reopened) == 1


def test_only_the_lock_module_reaches_pypdfium2() -> None:
    """The containment claim: there is nowhere else in `src/` to bypass the lock from."""
    source_root = Path(__file__).resolve().parents[2] / "src"
    lock_module = source_root / "crossfoot" / "pdfium.py"
    assert lock_module.is_file()
    offenders = sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.py")
        if path != lock_module and PDFIUM_LIBRARY in path.read_text(encoding="utf-8")
    )
    assert offenders == []
