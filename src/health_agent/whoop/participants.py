"""Explicit enrollment of additional people; never discover arbitrary sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from health_agent.automation.storage import require_private_file
from health_agent.config import Settings


@dataclass(frozen=True)
class DetailTarget:
    profile_id: UUID | None
    session_file: Path
    root: Path


def targets(settings: Settings) -> list[DetailTarget]:
    result = [
        DetailTarget(
            None, settings.whoop_detail_session_file, settings.whoop_detail_root
        )
    ]
    if settings.whoop_detail_accounts_file is not None:
        rows = json.loads(
            require_private_file(
                settings.whoop_detail_accounts_file.absolute()
            ).read_text()
        )
        if not isinstance(rows, list):
            raise ValueError("invalid_whoop_participants")
        for row in rows:
            result.append(
                DetailTarget(
                    UUID(row["profile_id"]),
                    Path(row["session_file"]),
                    Path(row["root"]),
                )
            )
    for paths in (
        [t.root.resolve() for t in result],
        [t.session_file.resolve() for t in result],
    ):
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate_whoop_participant_path")
    profiles = [t.profile_id for t in result if t.profile_id is not None]
    if len(set(profiles)) != len(profiles):
        raise ValueError("duplicate_whoop_participant_profile")
    return result
