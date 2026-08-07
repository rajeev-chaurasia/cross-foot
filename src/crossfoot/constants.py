"""Shared enums and named constants: LLM providers, domain vocabulary, grammars."""

from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------


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


PROVIDER_BASE_URLS: dict[Provider, str] = {
    Provider.GEMINI: "https://generativelanguage.googleapis.com/v1beta/openai",
    Provider.GROQ: "https://api.groq.com/openai/v1",
    Provider.OPENROUTER: "https://openrouter.ai/api/v1",
    Provider.MISTRAL: "https://api.mistral.ai/v1",
}

# Availability verified against each provider's model list on 2026-08-06.
# The openrouter default is vision-capable so the vision spillover chain
# stays usable when Gemini's daily cap is exhausted.
PROVIDER_DEFAULT_MODELS: dict[Provider, str] = {
    Provider.GEMINI: "gemini-3.5-flash",
    Provider.GROQ: "llama-3.3-70b-versatile",
    Provider.OPENROUTER: "nvidia/nemotron-nano-12b-v2-vl:free",
    Provider.MISTRAL: "mistral-small-latest",
}

# Call priority: vision extraction and spillover walk this order. Gemini leads
# because it is the vision-capable free tier the pipeline is designed around.
PROVIDER_PRIORITY: tuple[Provider, ...] = (
    Provider.GEMINI,
    Provider.GROQ,
    Provider.OPENROUTER,
    Provider.MISTRAL,
)


class Capability(StrEnum):
    """One thing a provider's default model must be able to do to serve a call."""

    VISION = "vision"
    JSON_SCHEMA = "json_schema"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What one provider's default model can do, as probed rather than assumed."""

    supports_vision: bool
    supports_json_schema: bool

    def supports(self, capability: Capability) -> bool:
        return {
            Capability.VISION: self.supports_vision,
            Capability.JSON_SCHEMA: self.supports_json_schema,
        }[capability]


# Probed on 2026-08-06, one direct call per provider carrying a tiny PNG and a
# json_schema response_format. Groq refused both ("messages[0].content must be a
# string", "This model does not support response format json_schema"), which is
# why it must never sit in a vision chain: 400 is a bad request, so it neither
# retries nor spills over and the document dies on the spot.
# Gemini's probe hit its spent daily quota, but 15 vision calls in the same run
# had already been served, so it is recorded as capable.
# Provider.CUSTOM is a user supplied gateway: the user chose the model behind
# it deliberately, so it is trusted with everything rather than probed here.
PROVIDER_CAPABILITIES: dict[Provider, ProviderCapabilities] = {
    Provider.CUSTOM: ProviderCapabilities(supports_vision=True, supports_json_schema=True),
    Provider.GEMINI: ProviderCapabilities(supports_vision=True, supports_json_schema=True),
    Provider.GROQ: ProviderCapabilities(supports_vision=False, supports_json_schema=False),
    Provider.OPENROUTER: ProviderCapabilities(supports_vision=True, supports_json_schema=True),
    Provider.MISTRAL: ProviderCapabilities(supports_vision=True, supports_json_schema=True),
}

# The vision extractor sends page images and demands structured output, so its
# pool needs both; a provider missing either cannot serve one document.
VISION_CAPABILITIES: tuple[Capability, ...] = (Capability.VISION, Capability.JSON_SCHEMA)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """One provider's pacing allowance, both dimensions per minute."""

    requests_per_minute: int
    tokens_per_minute: int


# Per provider because a shared limiter set to the slowest provider would
# throttle the fast ones for no reason. Conservative where a provider publishes
# nothing, since overshooting costs a whole daily quota.
PROVIDER_RATE_LIMITS: dict[Provider, RateLimit] = {
    # 10 rpm is Google's documented free tier figure for flash class models, and
    # exceeding it is why the 2026-08-06 run took 16 429s and then spent its
    # daily cap. Tokens are the published free tier per minute allowance.
    Provider.GEMINI: RateLimit(requests_per_minute=10, tokens_per_minute=250_000),
    # Phase 0 probe headers: 1000 requests and 12000 tokens over a roughly three
    # minute window, taken to a per minute rate and rounded below its share.
    Provider.GROQ: RateLimit(requests_per_minute=300, tokens_per_minute=4_000),
    # OpenRouter sent no rate limit headers, so this is a guess, not a figure.
    Provider.OPENROUTER: RateLimit(requests_per_minute=10, tokens_per_minute=100_000),
    # Phase 0 probe headers, reported per minute directly.
    Provider.MISTRAL: RateLimit(requests_per_minute=50, tokens_per_minute=50_000),
    # A user supplied gateway paces itself; this only stops a runaway loop.
    Provider.CUSTOM: RateLimit(requests_per_minute=60, tokens_per_minute=100_000),
}

CHAT_COMPLETIONS_PATH = "/chat/completions"

# Substrings that identify provider throttling headers, lowercased for matching.
RATE_LIMIT_HEADER_MARKERS = ("ratelimit", "retry-after", "quota")

# Price table version 2026-08-06. Values are (prompt, completion) list prices in
# microusd per million tokens, keyed by a model-name pattern matched as a
# case-insensitive substring; the longest matching pattern wins. Free tiers bill
# nothing, so the ledger stores this list-price equivalent beside the actual cost
# and the scorecard publishes a cost per document that means something. Entries
# not confirmed against the provider's public pricing page are marked below.
MODEL_LIST_PRICES_MICROUSD_PER_MTOK: dict[str, tuple[int, int]] = {
    "gemini-3.5-flash": (300_000, 2_500_000),  # unverified
    "gemini-3.5-pro": (1_250_000, 10_000_000),  # unverified
    "llama-3.3-70b-versatile": (590_000, 790_000),  # unverified
    "nemotron-nano-12b-v2-vl": (100_000, 400_000),  # unverified
    "mistral-small": (200_000, 600_000),  # unverified
}

# ---------------------------------------------------------------------------
# Domain vocabulary
# ---------------------------------------------------------------------------


class Oem(StrEnum):
    """Fictional marques, each styled after a real OEM's paperwork conventions.

    Real brand names and logos stay out of the synthetic documents on purpose;
    the formats are what carry the realism.
    """

    MERIDIAN = "meridian"  # Ford-style paperwork
    NORTHSTAR = "northstar"  # GM-style
    KAIZEN = "kaizen"  # Toyota-style
    ATLAS = "atlas"  # Stellantis-style


class DocType(StrEnum):
    PARTS_STATEMENT = "parts_statement"
    WARRANTY_CREDIT_MEMO = "warranty_credit_memo"
    FLOORPLAN_STATEMENT = "floorplan_statement"
    INCENTIVE_STATEMENT = "incentive_statement"


class ScheduleType(StrEnum):
    WARRANTY_RECEIVABLE = "warranty_receivable"
    PARTS_PAYABLE = "parts_payable"
    FLOORPLAN_LIABILITY = "floorplan_liability"
    INCENTIVE_RECEIVABLE = "incentive_receivable"


DOC_TYPE_SCHEDULES: dict[DocType, ScheduleType] = {
    DocType.PARTS_STATEMENT: ScheduleType.PARTS_PAYABLE,
    DocType.WARRANTY_CREDIT_MEMO: ScheduleType.WARRANTY_RECEIVABLE,
    DocType.FLOORPLAN_STATEMENT: ScheduleType.FLOORPLAN_LIABILITY,
    DocType.INCENTIVE_STATEMENT: ScheduleType.INCENTIVE_RECEIVABLE,
}


class QualityTier(StrEnum):
    CLEAN_DIGITAL = "clean_digital"
    SCAN_LIGHT = "scan_light"
    SCAN_HEAVY = "scan_heavy"
    CSV = "csv"
    XLSX = "xlsx"
    CORRUPTED = "corrupted"


class CorruptionKind(StrEnum):
    TRUNCATED_PDF = "truncated_pdf"
    WRONG_EXTENSION = "wrong_extension"
    EMPTY_FILE = "empty_file"
    ENCRYPTED_PDF = "encrypted_pdf"
    BINARY_JUNK = "binary_junk"


class LineType(StrEnum):
    CHARGE = "charge"
    CREDIT = "credit"
    ADJUSTMENT = "adjustment"
    PAYMENT = "payment"


class FieldName(StrEnum):
    STATEMENT_NUMBER = "statement_number"
    STATEMENT_DATE = "statement_date"
    TOTAL = "total"
    SUBTOTAL = "subtotal"
    PREVIOUS_BALANCE = "previous_balance"
    CLAIM_NUMBER = "claim_number"
    RO_NUMBER = "ro_number"
    VIN = "vin"
    INVOICE_NUMBER = "invoice_number"
    PROGRAM_CODE = "program_code"
    LINE_DATE = "line_date"
    LINE_AMOUNT = "line_amount"
    DESCRIPTION = "description"


class FieldFamily(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    REFERENCE = "reference"
    TEXT = "text"


FIELD_FAMILIES: dict[FieldName, FieldFamily] = {
    FieldName.STATEMENT_NUMBER: FieldFamily.REFERENCE,
    FieldName.STATEMENT_DATE: FieldFamily.DATE,
    FieldName.TOTAL: FieldFamily.AMOUNT,
    FieldName.SUBTOTAL: FieldFamily.AMOUNT,
    FieldName.PREVIOUS_BALANCE: FieldFamily.AMOUNT,
    FieldName.CLAIM_NUMBER: FieldFamily.REFERENCE,
    FieldName.RO_NUMBER: FieldFamily.REFERENCE,
    FieldName.VIN: FieldFamily.REFERENCE,
    FieldName.INVOICE_NUMBER: FieldFamily.REFERENCE,
    FieldName.PROGRAM_CODE: FieldFamily.REFERENCE,
    FieldName.LINE_DATE: FieldFamily.DATE,
    FieldName.LINE_AMOUNT: FieldFamily.AMOUNT,
    FieldName.DESCRIPTION: FieldFamily.TEXT,
}


class FieldSource(StrEnum):
    DETERMINISTIC = "deterministic"
    LLM_VISION = "llm_vision"
    HUMAN = "human"


class CropKind(StrEnum):
    EXACT_BBOX = "exact_bbox"
    ROW_BAND = "row_band"
    FULL_PAGE = "full_page"


class ReviewStatus(StrEnum):
    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_REVIEW = "needs_review"
    HUMAN_ACCEPTED = "human_accepted"
    HUMAN_CORRECTED = "human_corrected"


class ExtractionRoute(StrEnum):
    DIGITAL_PDF = "digital_pdf"
    SCANNED_PDF = "scanned_pdf"
    CSV = "csv"
    XLSX = "xlsx"
    UNPROCESSABLE = "unprocessable"


class IngestErrorKind(StrEnum):
    TRUNCATED = "truncated"
    ENCRYPTED = "encrypted"
    EMPTY = "empty"
    UNRECOGNIZED = "unrecognized"
    TOO_LARGE = "too_large"  # over the extractor's file size or row ceiling


class ExceptionType(StrEnum):
    MISSING_FROM_LEDGER = "missing_from_ledger"
    MISSING_FROM_STATEMENT = "missing_from_statement"
    AMOUNT_MISMATCH = "amount_mismatch"
    DUPLICATE = "duplicate"
    SHORT_PAY = "short_pay"
    TIMING_DIFFERENCE = "timing_difference"


class ExceptionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReconMode(StrEnum):
    END_TO_END = "end_to_end"
    ORACLE = "oracle"


class SplitName(StrEnum):
    TRAIN = "train"
    CALIBRATION = "calibration"
    TEST = "test"


# ---------------------------------------------------------------------------
# Reference-number grammars
# ---------------------------------------------------------------------------

# Synthetic per-marque formats modeled on real OEM conventions. The generator
# emits values matching these patterns and the confidence validators check
# against the same table, which is honest because the dataset is synthetic
# and says so.
REF_GRAMMARS: dict[Oem, dict[FieldName, str]] = {
    Oem.MERIDIAN: {
        FieldName.CLAIM_NUMBER: r"\d{4}[A-Z]\d{5}",
        FieldName.RO_NUMBER: r"RO\d{6}",
        FieldName.INVOICE_NUMBER: r"M\d{7}",
        FieldName.PROGRAM_CODE: r"PGM-\d{4}",
    },
    Oem.NORTHSTAR: {
        FieldName.CLAIM_NUMBER: r"NS\d{8}",
        FieldName.RO_NUMBER: r"\d{6}",
        FieldName.INVOICE_NUMBER: r"INV\d{7}",
        FieldName.PROGRAM_CODE: r"NS-[A-Z]{2}\d{3}",
    },
    Oem.KAIZEN: {
        FieldName.CLAIM_NUMBER: r"K\d{3}-\d{6}",
        FieldName.RO_NUMBER: r"RO-\d{6}",
        FieldName.INVOICE_NUMBER: r"\d{8}",
        FieldName.PROGRAM_CODE: r"KZN\d{4}",
    },
    Oem.ATLAS: {
        FieldName.CLAIM_NUMBER: r"AT\d{7}",
        FieldName.RO_NUMBER: r"R\d{7}",
        FieldName.INVOICE_NUMBER: r"AX\d{6}",
        FieldName.PROGRAM_CODE: r"ATLAS-\d{3}",
    },
}

# ---------------------------------------------------------------------------
# CSV header vocabulary
# ---------------------------------------------------------------------------

# Shared knowledge between the tabular renderer (which rotates through these)
# and the extractor (which matches them case-insensitively). Lives here, not in
# generator, so the extraction import boundary stays clean.
CSV_HEADER_SYNONYMS: dict[FieldName, tuple[str, ...]] = {
    FieldName.CLAIM_NUMBER: ("Claim Number", "CLAIM_NO", "ClaimNbr", "Claim #"),
    FieldName.RO_NUMBER: ("RO Number", "RepairOrder", "RO #", "RO_NO"),
    FieldName.VIN: ("VIN", "Vin #", "Vehicle ID"),
    FieldName.INVOICE_NUMBER: ("Invoice Number", "Invoice #", "INV_NO", "InvoiceNbr"),
    FieldName.PROGRAM_CODE: ("Program Code", "Program", "PGM_CD"),
    FieldName.LINE_DATE: ("Date", "Post Dt", "Post Date", "Trans Date"),
    FieldName.DESCRIPTION: ("Description", "Desc", "Detail"),
    FieldName.LINE_AMOUNT: ("Amount", "Amt", "Net Amount", "AMOUNT_USD"),
}

# ---------------------------------------------------------------------------
# VIN check digit (ISO 3779)
# ---------------------------------------------------------------------------

VIN_LENGTH = 17
VIN_CHECK_DIGIT_INDEX = 8
VIN_POSITION_WEIGHTS: tuple[int, ...] = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
VIN_CHAR_VALUES: dict[str, int] = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}  # fmt: skip
