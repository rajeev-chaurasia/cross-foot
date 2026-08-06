"""Chromium-printed PDF renderer over the Jinja2 marque template families."""

import hashlib
from pathlib import Path
from types import TracebackType

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from playwright.sync_api import Browser, Playwright, sync_playwright
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject

from crossfoot.constants import FieldName
from crossfoot.generator.renderers.base import (
    DOC_LINE_FIELDS,
    DOC_TITLES,
    MARQUE_BRANDING,
    PAYABLE_DOC_TYPES,
    format_due_date,
    format_marque_amount,
    format_marque_date,
    format_marque_line_no,
    header_key,
    line_key,
    line_reference,
    marque_address,
    remit_address,
)
from crossfoot.models.statement import StatementDoc

# templates/ lives at the repo root; resolve relative to this module so any cwd works.
TEMPLATES_DIR = Path(__file__).resolve().parents[4] / "templates"
TEMPLATE_SUFFIX = ".html.j2"
PDF_PAGE_FORMAT = "Letter"
PDF_MARGIN = "0.5in"
# Chromium stamps the wall clock into /CreationDate and /ModDate and leaves the
# trailer id to chance; both are pinned so the same seed yields the same bytes.
PDF_FIXED_DATE = "D:20260101000000+00'00'"
PDF_TRAILER_ID_BYTES = 16


def _template_relpath(template_id: str) -> str:
    """Map '{oem}-{doc_type}-vN' to '{oem}/{doc_type}.html.j2'.

    The version suffix names the variant in the manifest; v1 is the family's
    base file, and future designed variants map to their own files.
    """
    oem, _, rest = template_id.partition("-")
    doc_type, _, version = rest.rpartition("-")
    if not oem or not doc_type or not version.startswith("v"):
        raise ValueError(f"unrecognized template_id {template_id!r}")
    return f"{oem}/{doc_type}{TEMPLATE_SUFFIX}"


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=StrictUndefined,
    )


def build_context(doc: StatementDoc) -> tuple[dict[str, object], dict[str, str]]:
    """Frozen template context (all values pre-formatted strings) plus rendered_values."""
    branding = MARQUE_BRANDING[doc.oem]
    statement_date = format_marque_date(doc.oem, doc.statement_date)
    subtotal = format_marque_amount(doc.oem, doc.subtotal_cents, in_totals=True)
    total = format_marque_amount(doc.oem, doc.total_cents, in_totals=True)
    previous_balance = (
        ""
        if doc.previous_balance_cents is None
        else format_marque_amount(doc.oem, doc.previous_balance_cents, in_totals=True)
    )
    adjustments = (
        ""
        if doc.adjustments_cents == 0
        else format_marque_amount(doc.oem, doc.adjustments_cents, in_totals=True)
    )
    rendered: dict[str, str] = {
        header_key(FieldName.STATEMENT_NUMBER): doc.statement_number,
        header_key(FieldName.STATEMENT_DATE): statement_date,
        header_key(FieldName.SUBTOTAL): subtotal,
        header_key(FieldName.TOTAL): total,
    }
    if previous_balance:
        rendered[header_key(FieldName.PREVIOUS_BALANCE)] = previous_balance

    shown_fields = DOC_LINE_FIELDS[doc.doc_type]
    lines_context: list[dict[str, str]] = []
    for line in doc.lines:
        line_date = format_marque_date(doc.oem, line.line_date)
        amount = format_marque_amount(doc.oem, line.amount_cents)
        lines_context.append(
            {
                "line_no": format_marque_line_no(doc.oem, line.line_no),
                "line_date": line_date,
                "claim_number": line.claim_number or "",
                "ro_number": line.ro_number or "",
                "vin": line.vin or "",
                "invoice_number": line.invoice_number or "",
                "program_code": line.program_code or "",
                "description": line.description,
                "amount": amount,
            }
        )
        for field in shown_fields:
            if field is FieldName.LINE_DATE:
                value = line_date
            elif field is FieldName.LINE_AMOUNT:
                value = amount
            elif field is FieldName.DESCRIPTION:
                value = line.description
            else:
                value = line_reference(line, field) or ""
            if value:
                rendered[line_key(line.line_no, field)] = value

    context: dict[str, object] = {
        "marque_name": branding.name,
        "marque_tagline": branding.tagline,
        "marque_address": marque_address(doc.oem, doc.doc_type),
        "dealer_name": branding.dealer_name,
        "dealer_code": branding.dealer_code,
        "dealer_address": branding.dealer_address,
        "doc_title": DOC_TITLES[doc.doc_type],
        "statement_number": doc.statement_number,
        "statement_date": statement_date,
        "period_start": format_marque_date(doc.oem, doc.period_start),
        "period_end": format_marque_date(doc.oem, doc.period_end),
        "previous_balance": previous_balance,
        "subtotal": subtotal,
        "adjustments": adjustments,
        "total": total,
        "lines": lines_context,
    }
    if doc.doc_type in PAYABLE_DOC_TYPES:
        # Frozen contract: payable doc types also carry remittance instructions.
        context["due_date"] = format_due_date(doc.oem, doc.statement_date)
        context["remit_address"] = remit_address(doc.oem, doc.doc_type)
    return context, rendered


def pin_pdf_metadata(path: Path, doc_id: str) -> None:
    """Rewrite a printed PDF with fixed dates and a doc_id-derived trailer id."""
    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({"/CreationDate": PDF_FIXED_DATE, "/ModDate": PDF_FIXED_DATE})
    digest = hashlib.sha256(doc_id.encode("utf-8")).digest()[:PDF_TRAILER_ID_BYTES]
    identifier = ByteStringObject(digest)
    # pypdf exposes the trailer id only as an attribute; leaving it unset writes
    # no /ID at all, which readers dislike more than a synthetic one.
    writer._ID = ArrayObject([identifier, identifier])
    with path.open("wb") as handle:
        writer.write(handle)


def render_html(doc: StatementDoc, template_id: str) -> tuple[str, dict[str, str]]:
    """Render the template to HTML without printing; returns (html, rendered_values)."""
    template = _environment().get_template(_template_relpath(template_id))
    context, rendered = build_context(doc)
    return template.render(context), rendered


class ChromiumPdfRenderer:
    """Prints statement HTML through headless Chromium into born-digital PDFs.

    Use as a context manager to share one browser across many render() calls;
    render() lazily starts the browser when used standalone.
    """

    def __init__(self) -> None:
        self._env = _environment()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> "ChromiumPdfRenderer":
        self._ensure_browser()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _ensure_browser(self) -> Browser:
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        return self._browser

    def render(
        self, doc: StatementDoc, template_id: str, seed: int, out_path: Path
    ) -> dict[str, str]:
        """Print one statement PDF; v1 templates take no seeded variation."""
        template = self._env.get_template(_template_relpath(template_id))
        context, rendered = build_context(doc)
        html = template.render(context)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        page = self._ensure_browser().new_page()
        try:
            page.set_content(html)
            page.pdf(
                path=str(out_path),
                format=PDF_PAGE_FORMAT,
                print_background=True,
                margin={
                    "top": PDF_MARGIN,
                    "right": PDF_MARGIN,
                    "bottom": PDF_MARGIN,
                    "left": PDF_MARGIN,
                },
            )
        finally:
            page.close()
        pin_pdf_metadata(out_path, doc.doc_id)
        return rendered
