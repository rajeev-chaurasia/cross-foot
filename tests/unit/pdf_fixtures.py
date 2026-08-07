"""A hand-built text PDF, so the digital-pdf tests need no browser.

The bytes are assembled directly rather than printed, which keeps the tests
offline and makes every word position an exact, asserted number.
"""

from datetime import date

from crossfoot.constants import DocType, LineType, Oem
from crossfoot.models.statement import StatementDoc, StatementLine

PAGE_WIDTH = 612
PAGE_HEIGHT = 792
FONT_SIZE = 9

TRUTH_DOC = StatementDoc(
    doc_id="doc-parts_statement-dlr-meridian-202607-01",
    dealer_id="dlr-meridian",
    doc_type=DocType.PARTS_STATEMENT,
    oem=Oem.MERIDIAN,
    statement_number="PS-2026-07-001",
    statement_date=date(2026, 7, 31),
    period_start=date(2026, 7, 1),
    period_end=date(2026, 7, 31),
    previous_balance_cents=20_000,
    subtotal_cents=125_000,
    total_cents=145_000,
    lines=(
        StatementLine(
            line_no=1,
            line_type=LineType.CHARGE,
            invoice_number="M1234567",
            line_date=date(2026, 7, 10),
            description="Brake pads",
            amount_cents=100_000,
        ),
        StatementLine(
            line_no=2,
            line_type=LineType.CHARGE,
            invoice_number="M7654321",
            line_date=date(2026, 7, 18),
            description="Oil filters",
            amount_cents=25_000,
        ),
    ),
)

# (x, baseline y from the page bottom, size, text)
TextItem = tuple[float, float, int, str]


def statement_items(doc: StatementDoc) -> list[TextItem]:
    """A parts statement laid out the way the meridian template prints one."""
    items: list[TextItem] = [
        (400, 720, FONT_SIZE, f"Statement {doc.statement_number}"),
        (400, 706, FONT_SIZE, "Issued 07/31/2026"),
        (50, 600, FONT_SIZE, "Date"),
        (140, 600, FONT_SIZE, "Invoice"),
        (240, 600, FONT_SIZE, "Description"),
        (500, 600, FONT_SIZE, "Amount"),
    ]
    top = 580
    for line in doc.lines:
        items.extend(
            [
                (50, top, FONT_SIZE, f"{line.line_date:%m/%d/%Y}"),
                (140, top, FONT_SIZE, line.invoice_number or ""),
                (240, top, FONT_SIZE, line.description),
                (490, top, FONT_SIZE, f"${line.amount_cents / 100:,.2f}"),
            ]
        )
        top -= 20
    items.extend(
        [
            (400, 500, FONT_SIZE, "Previous balance"),
            (490, 500, FONT_SIZE, "$200.00"),
            (400, 486, FONT_SIZE, "Subtotal"),
            (490, 486, FONT_SIZE, "$1,250.00"),
            (400, 470, FONT_SIZE, "Total due"),
            (490, 470, FONT_SIZE, "$1,450.00"),
        ]
    )
    return items


def minimal_pdf(items: list[TextItem]) -> bytes:
    """One page of positioned Helvetica text, as a well-formed PDF."""
    operators = "\n".join(
        f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_escape(text)}) Tj ET" for x, y, size, text in items
    )
    stream = operators.encode("latin-1")
    objects: tuple[bytes, ...] = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>" % (PAGE_WIDTH, PAGE_HEIGHT),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
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


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
