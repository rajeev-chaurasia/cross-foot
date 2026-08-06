"""Values crossing the LLM boundary: usage, results, page images, errors.

They live apart from the client so cassettes and the response cache can build
and return them without importing the client back.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

PNG_DATA_URI_PREFIX = "data:image/png;base64,"


@dataclass(frozen=True)
class ChatUsage:
    """Token counts exactly as the provider reported them, never recomputed."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: ChatUsage
    # Transport metadata, excluded from equality. Cassette scrubbing drops the
    # throttling headers and replay measures no latency, so identity is content,
    # model, and usage: a replayed result equals the result that recorded it.
    latency_ms: int = field(compare=False)
    rate_limit_headers: dict[str, str] = field(compare=False)


@dataclass(frozen=True)
class PageImage:
    """One rasterized page, sent as an OpenAI-style image_url content part."""

    page_index: int
    png_bytes: bytes

    def data_uri(self) -> str:
        return PNG_DATA_URI_PREFIX + base64.b64encode(self.png_bytes).decode("ascii")

    def digest(self) -> str:
        """Cassette keys hash the bytes, so the base64 rendering never keys a call."""
        return hashlib.sha256(self.png_bytes).hexdigest()


class LlmError(RuntimeError):
    """Raised when the provider returns a non-success response.

    status_code is None when the request never produced an HTTP status, which
    the spillover pool reads as a provider that is down rather than a bad
    request.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
