"""Shared enums and named constants. Domain enums land with the Phase 1 contracts."""

from enum import StrEnum


class LlmMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class Provider(StrEnum):
    CUSTOM = "custom"
    GEMINI = "gemini"
    GROQ = "groq"
    OPENROUTER = "openrouter"
    MISTRAL = "mistral"
    CEREBRAS = "cerebras"


PROVIDER_BASE_URLS: dict[Provider, str] = {
    Provider.GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
    Provider.GROQ: "https://api.groq.com/openai/v1",
    Provider.OPENROUTER: "https://openrouter.ai/api/v1",
    Provider.MISTRAL: "https://api.mistral.ai/v1",
    Provider.CEREBRAS: "https://api.cerebras.ai/v1",
}

PROVIDER_DEFAULT_MODELS: dict[Provider, str] = {
    Provider.GEMINI: "gemini-2.5-flash",
    Provider.GROQ: "llama-3.3-70b-versatile",
    Provider.OPENROUTER: "meta-llama/llama-3.3-70b-instruct:free",
    Provider.MISTRAL: "mistral-small-latest",
    Provider.CEREBRAS: "llama-3.3-70b",
}

# Call priority: vision extraction and spillover walk this order. Gemini leads
# because it is the vision-capable free tier the pipeline is designed around.
PROVIDER_PRIORITY: tuple[Provider, ...] = (
    Provider.GEMINI,
    Provider.GROQ,
    Provider.OPENROUTER,
    Provider.MISTRAL,
    Provider.CEREBRAS,
)

CHAT_COMPLETIONS_PATH = "/chat/completions"

# Substrings that identify provider throttling headers, lowercased for matching.
RATE_LIMIT_HEADER_MARKERS = ("ratelimit", "retry-after", "quota")
