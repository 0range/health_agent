"""Immutable, content-free contracts used by the automation layer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

AutomationSource = Literal[
    "whoop",
    "gmail",
    "drive",
    "lab_extraction",
    "sheets",
    "calendar",
    "dashboard",
]
AutomationMode = Literal["full", "incremental", "none"]
AutomationStatus = Literal["succeeded", "deferred", "failed", "timed_out", "skipped"]

_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.:@+-]{1,128}")


def _validated(value: str, field: str) -> str:
    if _SAFE_VALUE.fullmatch(value) is None:
        raise ValueError(f"unsafe {field}")
    return value


@dataclass(frozen=True, slots=True)
class AutomationJob:
    source: AutomationSource
    profile_id: str
    account_id: str
    supports_full: bool
    arguments: tuple[str, ...]
    not_ready_code: str | None = None

    def __post_init__(self) -> None:
        _validated(self.source, "source")
        _validated(self.profile_id, "profile ID")
        _validated(self.account_id, "account ID")
        if not self.arguments or any(
            "\n" in value or "\r" in value for value in self.arguments
        ):
            raise ValueError("unsafe connector arguments")
        if self.not_ready_code is not None:
            _validated(self.not_ready_code, "not-ready code")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.profile_id, self.account_id


@dataclass(frozen=True, slots=True)
class AutomationResult:
    source: str
    profile_id: str
    account_id: str
    mode: AutomationMode
    status: AutomationStatus
    safe_error_code: str | None = None

    def safe_line(self) -> str:
        values = (
            ("source", self.source),
            ("profile", self.profile_id),
            ("account", self.account_id),
            ("mode", self.mode),
            ("status", self.status),
            ("safe_error", self.safe_error_code or "none"),
        )
        return " ".join(f"{name}={_validated(value, name)}" for name, value in values)
