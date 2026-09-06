from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

MAX_PAGE_CHARACTERS = 60_000
MAX_CLOUD_CHARACTERS = 12_000
MAX_CANDIDATES = 40
EXTRACTOR_VERSION = "lab-extraction-v1"

SAFE_CODES = frozenset(
    [
        "extraction_failed",
        "extraction_busy",
        "extraction_not_configured",
        "invalid_daily_budget",
        "invalid_run_limit",
        "invalid_status_limit",
        "profile_not_found",
        "document_not_found",
        "stale_extraction_claim",
        "unsafe_extraction_path",
        "vault_integrity",
        "original_size_limit",
        "page_text_limit",
        "unreadable_original",
        "unsupported_original",
        "original_mime_mismatch",
        "page_pixel_limit",
        "ocr_unavailable",
        "local_extraction_failed",
        "local_validation_failed",
        "no_page_text",
        "page_evidence_changed",
        "page_evidence_exists",
        "candidate_limit",
        "page_limit",
        "cloud_attempt_limit",
        "extraction_disabled",
        "cloud_budget_or_optin_required",
        "openai_not_configured",
        "yandex_not_configured",
        "cloud_provider_consent_required",
        "cloud_input_limit",
        "cloud_quota_exhausted",
        "cloud_rate_limited",
        "cloud_auth_required",
        "cloud_request_rejected",
        "cloud_outcome_unknown",
        "cloud_unknown_acknowledged",
        "cloud_incomplete",
        "cloud_invalid_output",
        "cloud_refused",
        "unknown_retry_requires_acknowledgment",
    ]
)

CLOUD_RUN_STOP_CODES = frozenset(
    ["cloud_quota_exhausted", "cloud_rate_limited", "cloud_auth_required"]
)


def declared_safe_code(value: str | None) -> str:
    return value if value in SAFE_CODES else "extraction_failed"


class ExtractionError(ValueError):
    """Only an application-owned safe code crosses the extraction boundary."""

    def __init__(self, safe_code: str) -> None:
        self.safe_code = declared_safe_code(safe_code)
        super().__init__(self.safe_code)


@dataclass(frozen=True, slots=True)
class Candidate:
    source_name: str
    source_value: str
    source_unit: str
    reference_text: str | None
    evidence_excerpt: str
    canonical_name: str
    parsed_value: Decimal | None
    source_flag: str | None = None
    reference_low: Decimal | None = None
    reference_high: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LocalResult:
    candidates: tuple[Candidate, ...]
    unresolved: bool


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    id: UUID
    profile_id: UUID
    sha256: str
    vault_path: str
    media_type: str
