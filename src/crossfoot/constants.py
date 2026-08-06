"""Shared enums and named constants. Domain enums land with the Phase 1 contracts."""

from enum import StrEnum


class LlmMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


CHAT_COMPLETIONS_PATH = "/chat/completions"

# Substrings that identify provider throttling headers, lowercased for matching.
RATE_LIMIT_HEADER_MARKERS = ("ratelimit", "retry-after", "quota")
