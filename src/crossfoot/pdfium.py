"""Every PDFium call in this process, behind one lock.

PDFium is not thread safe. FastAPI runs sync `def` handlers in a threadpool, so
the review queue asking for one crop per field puts several threads inside the
library at once, and that faults the process: rasterizing with six concurrent
workers raised an illegal instruction (WinError 0xc000001d) out of the render
call, after which every later request failed and uvicorn exited. A crash takes
the whole server down, so correctness wins over throughput here and the render
path is serialized.

The lock covers opening, rendering and closing rather than the render alone,
because all three are calls into the same unguarded library, and the whole
document scope is the unit callers actually work in. Serving crops one at a
time is the intended cost: the render is paid once per field and every later
request is a file read from the crop cache.

Anything a caller pulls out of a document has to be copied into its own memory
before the scope ends. `to_pil` and `to_numpy` hand back views onto a bitmap
buffer PDFium owns, and that buffer is neither valid nor safe to touch after
the document closes and the lock is released.

This module is the only place in `src/` that imports pypdfium2, which is what
makes the lock impossible to route around by accident. A unit test enforces it.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pypdfium2

# Process wide, because the constraint is a property of the library and not of
# any one document, path or request.
_PDFIUM_LOCK = threading.Lock()

# Re-exported so a caller can name the failure PDFium raises without importing
# the library and stepping outside the lock to do it.
PdfiumError = pypdfium2.PdfiumError


@contextmanager
def open_document(path: Path) -> Iterator[Any]:
    """One document, opened, used and closed with the PDFium lock held throughout.

    The document is closed and the lock released however the body ends, so a
    render that raises inside the scope leaves nothing for the next caller to
    trip over.
    """
    with _PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(path)
        try:
            yield document
        finally:
            document.close()
