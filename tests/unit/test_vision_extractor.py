"""Vision extractor details the contract does not pin: rasterizing and hints.

Driven by a fake client with canned answers, so nothing here touches a network
or a key.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from pdf_fixtures import TRUTH_DOC, minimal_pdf, statement_items
from PIL import Image

from crossfoot.constants import DocType, FieldName, QualityTier
from crossfoot.extraction import llm_vision
from crossfoot.extraction.llm_vision import (
    MAX_IMAGE_EDGE_PX,
    PageImage,
    VisionExtractor,
    rasterize_pdf,
    response_model_for,
)
from crossfoot.llm.results import ChatResult, ChatUsage
from crossfoot.models.extraction import ExtractedDocument

DOC_ID = "doc-parts_statement-dlr-meridian-202607-01"
PNG_BYTES = b"\x89PNG\r\n\x1a\n"

PAYLOAD: dict[str, Any] = {
    "statement_number": {"raw": "PS-2026-07-001", "normalized": "PS-2026-07-001"},
    "total": {"raw": "$1,450.00", "normalized": "1450.00"},
    "lines": [
        {
            "row_position": 1,
            "invoice_number": {"raw": "M1234567", "normalized": "M1234567"},
            "line_date": {"raw": "07/10/2026", "normalized": "2026-07-10"},
            "line_amount": {"raw": "$1,000.00", "normalized": "1000.00"},
            "bbox": [10, 200, 990, 230],
        }
    ],
}


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_vision(self, *args: Any, **kwargs: Any) -> ChatResult:
        del args
        self.calls.append(kwargs)
        return ChatResult(
            content=self._responses.pop(0),
            model="fake-vision",
            usage=ChatUsage(prompt_tokens=1, completion_tokens=1, total_tokens=1),
            latency_ms=0,
            rate_limit_headers={},
        )


def _payload(**overrides: Any) -> str:
    payload = json.loads(json.dumps(PAYLOAD))
    payload.update(overrides)
    return json.dumps(payload)


async def _extract(client: FakeClient) -> ExtractedDocument:
    return await VisionExtractor(client).extract_document(
        doc_id=DOC_ID,
        file_path="files/statement.pdf",
        doc_type=DocType.PARTS_STATEMENT,
        quality_tier=QualityTier.SCAN_LIGHT,
        images=(PageImage(page=0, png_bytes=PNG_BYTES),),
    )


def _line_field(doc: ExtractedDocument, name: FieldName) -> Any:
    return next(field for field in doc.line_fields if field.name is name)


# ---------------------------------------------------------------------------
# Rasterizing
# ---------------------------------------------------------------------------


def test_rasterizing_caps_the_longest_edge(tmp_path: Path) -> None:
    path = tmp_path / "statement.pdf"
    path.write_bytes(minimal_pdf(statement_items(TRUTH_DOC)))
    pages = rasterize_pdf(path)
    assert len(pages) == 1
    assert pages[0].page == 0

    # The cap bounds image tokens, which is what a free tier actually rations.
    assert max(Image.open(io.BytesIO(pages[0].png_bytes)).size) == MAX_IMAGE_EDGE_PX


def test_page_image_converts_to_the_wire_type() -> None:
    wire = PageImage(page=3, png_bytes=PNG_BYTES).to_client_image()
    assert wire.page_index == 3
    assert wire.png_bytes == PNG_BYTES


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def test_line_field_names_follow_the_doc_type() -> None:
    assert llm_vision.line_field_names(DocType.PARTS_STATEMENT) == (
        FieldName.INVOICE_NUMBER,
        FieldName.LINE_DATE,
        FieldName.DESCRIPTION,
        FieldName.LINE_AMOUNT,
    )
    assert FieldName.PROGRAM_CODE in llm_vision.line_field_names(DocType.INCENTIVE_STATEMENT)


def test_a_doc_type_model_rejects_another_type_s_columns() -> None:
    model = response_model_for(DocType.PARTS_STATEMENT)
    parsed = model.model_validate(json.loads(_payload()))
    assert not hasattr(parsed.lines[0], FieldName.PROGRAM_CODE.value)


# ---------------------------------------------------------------------------
# Coordinate hints
# ---------------------------------------------------------------------------


async def test_a_sane_bbox_is_kept_in_the_normalized_frame() -> None:
    doc = await _extract(FakeClient([_payload(), _payload()]))
    bbox = _line_field(doc, FieldName.LINE_AMOUNT).bbox
    assert bbox is not None
    assert (bbox.x0, bbox.x1) == (0.01, 0.99)
    assert bbox.page == 0


@pytest.mark.parametrize(
    "bad",
    [
        [10, 200, 5, 230],  # inverted
        [10, 200, 990, 1400],  # outside the frame
        [10, 200, 30, 900],  # tall and narrow, not a table row
    ],
)
async def test_an_implausible_bbox_is_discarded_silently(bad: list[int]) -> None:
    payload = json.loads(_payload())
    payload["lines"][0]["bbox"] = bad
    text = json.dumps(payload)
    doc = await _extract(FakeClient([text, text]))
    assert _line_field(doc, FieldName.LINE_AMOUNT).bbox is None


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


async def test_the_warm_sample_shuffles_the_field_order_deterministically() -> None:
    first = FakeClient([_payload(), _payload()])
    second = FakeClient([_payload(), _payload()])
    await _extract(first)
    await _extract(second)
    prompts = [
        call["messages"][-1]["content"] for client in (first, second) for call in client.calls
    ]
    assert prompts[0] != prompts[1]  # the warm sample reorders the field list
    assert prompts[:2] == prompts[2:]  # seeded by doc_id, so the run repeats


async def test_a_field_missing_from_the_warm_sample_disagrees() -> None:
    without_total = json.loads(_payload())
    del without_total["total"]
    doc = await _extract(FakeClient([_payload(), json.dumps(without_total)]))
    total = next(field for field in doc.header_fields if field.name is FieldName.TOTAL)
    assert total.signals.self_consistency == 0.0


async def test_images_reach_the_client_as_wire_page_images() -> None:
    client = FakeClient([_payload(), _payload()])
    await _extract(client)
    images = client.calls[0]["images"]
    assert [image.page_index for image in images] == [0]
    assert images[0].png_bytes == PNG_BYTES
