"""Bounded post-sync orchestration; connectors never own an external-call transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine

from health_agent.config import Settings
from health_agent.lab_extraction.local import read_page
from health_agent.lab_extraction.openai import OpenAILabExtractor
from health_agent.lab_extraction.queue import ExtractionQueue, QueueStatus, profile_lock
from health_agent.lab_extraction.types import (
    MAX_CLOUD_CHARACTERS,
    Candidate,
    DocumentSnapshot,
    ExtractionError,
)
from health_agent.lab_extraction.validation import parse_local


class CloudExtractor(Protocol):
    def extract(self, profile_id: UUID, text: str) -> tuple[Candidate, ...]: ...


LocalReader = Callable[[DocumentSnapshot, int, Path, Path], str]


@dataclass(frozen=True, slots=True)
class RunReport:
    status: str
    processed: int = 0
    inserted: int = 0
    cloud_requests: int = 0
    waiting_cloud: int = 0
    attention: int = 0


class LabExtractionService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        *,
        cloud_extractor: CloudExtractor | None = None,
        clock: Callable[[], datetime] | None = None,
        local_reader: LocalReader | None = None,
    ) -> None:
        self.engine, self.settings = engine, settings
        self.queue = ExtractionQueue(engine)
        self.cloud = cloud_extractor or OpenAILabExtractor(settings)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.local = local_reader or (
            lambda snapshot, page, vault, temporary: read_page(
                snapshot, page, vault_root=vault, temporary_root=temporary
            )
        )

    def configure(
        self,
        profile_id: UUID,
        *,
        enabled: bool = True,
        openai: bool = False,
        daily_budget: int = 20,
    ) -> None:
        self.queue.configure(
            profile_id, enabled=enabled, openai=openai, daily_budget=daily_budget
        )

    def status(self, profile_id: UUID) -> QueueStatus:
        return self.queue.status(profile_id, self.clock().astimezone(UTC).date())

    def retry(
        self, profile_id: UUID, document_id: UUID, *, acknowledge_unknown: bool = False
    ) -> int:
        return self.queue.retry(
            profile_id, document_id, acknowledge_unknown=acknowledge_unknown
        )

    def run(
        self, profile_id: UUID, *, limit: int = 4, cloud_limit: int = 2
    ) -> RunReport:
        if not 1 <= limit <= 20 or not 0 <= cloud_limit <= 10:
            raise ExtractionError("invalid_run_limit")
        with profile_lock(self.engine, profile_id):
            state = self.status(profile_id)
            if not state.configured:
                raise ExtractionError("extraction_not_configured")
            if not state.enabled:
                return RunReport("deferred")
            self.queue.discover_and_recover(profile_id)
            processed = inserted = requests = 0
            for job_id in self.queue.pending(
                profile_id, limit, cloud=state.cloud_enabled
            ):
                claim = self.queue.claim(profile_id, job_id)
                processed += 1
                if claim.token.int == 0:
                    continue
                reserved = False
                try:
                    source_text = claim.text or self.local(
                        claim.document,
                        claim.page_number,
                        self.settings.vault_root,
                        self.settings.temporary_root,
                    )
                    local = parse_local(source_text)
                    inserted += self.queue.publish(
                        claim,
                        source_text,
                        local.candidates,
                        cloud=False,
                        unresolved=local.unresolved,
                    )
                    if not local.unresolved:
                        continue
                    if not source_text.strip():
                        raise ExtractionError("no_page_text")
                    if len(source_text) > MAX_CLOUD_CHARACTERS:
                        raise ExtractionError("cloud_input_limit")
                    reserved = self.queue.reserve_cloud(
                        claim,
                        self.clock().astimezone(UTC).date(),
                        self.settings.openai_model,
                        allowed=requests < cloud_limit,
                    )
                    if not reserved:
                        continue
                    requests += 1
                    candidates = self.cloud.extract(profile_id, source_text)
                    inserted += self.queue.publish(
                        claim, source_text, candidates, cloud=True
                    )
                except ExtractionError as error:
                    self.queue.fail(claim, error.safe_code)
                except ValueError:
                    self.queue.fail(
                        claim,
                        "cloud_invalid_output"
                        if reserved
                        else "local_validation_failed",
                    )
                except Exception:  # noqa: BLE001 -- no source text/native/SDK exception leakage
                    self.queue.fail(
                        claim,
                        "cloud_outcome_unknown"
                        if reserved
                        else "local_extraction_failed",
                    )
            state = self.status(profile_id)
            return RunReport(
                "deferred"
                if state.waiting_cloud or state.attention or state.queued
                else "succeeded",
                processed,
                inserted,
                requests,
                state.waiting_cloud,
                state.attention,
            )
