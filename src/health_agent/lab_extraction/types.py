from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

MAX_PAGE_CHARACTERS = 60_000
MAX_CLOUD_CHARACTERS = 12_000
MAX_CANDIDATES = 40
EXTRACTOR_VERSION = "lab-extraction-v1"


class ExtractionError(ValueError):
    """Only an application-owned safe code crosses the extraction boundary."""

    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


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
