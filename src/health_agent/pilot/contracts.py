"""Shared I/O contracts; domain coaches own their behaviour, not infrastructure."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class Record:
    id: str
    domain: str
    kind: str
    source_key: str
    at: datetime
    payload: dict[str, Any]


class Store(Protocol):
    def put(
        self, profile_id: UUID, domain: str, kind: str, source_key: str,
        payload: dict[str, Any], *, at: datetime | None = None,
    ) -> Record: ...

    def list(
        self, profile_id: UUID, domain: str, kind: str | None = None,
        *, limit: int = 100,
    ) -> list[Record]: ...

    def get(self, profile_id: UUID, record_id: str) -> Record | None: ...

    def patch(
        self, profile_id: UUID, record_id: str, payload: dict[str, Any],
    ) -> Record: ...


class Brain(Protocol):
    def __call__(
        self, system: str, payload: dict[str, Any], *, image_path: Path | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class Attachment:
    path: Path
    media_type: str
    caption: str = ""


@dataclass(frozen=True)
class Notice:
    key: str
    text: str


class Coach(Protocol):
    def handle(
        self, profile_id: UUID, text: str, *, source_key: str, now: datetime,
        attachment: Attachment | None = None,
    ) -> str: ...

    def due(self, profile_id: UUID, now: datetime) -> list[Notice]: ...


HealthContext = Callable[[UUID, str], dict[str, Any]]
ActivitySource = Callable[[UUID, datetime, datetime], list[dict[str, Any]]]
