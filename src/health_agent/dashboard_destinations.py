"""Private profile-bound local pointers to provisioned Metabase dashboards."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


class DashboardDestinationStore:
    def __init__(self, root: Path, origin: str) -> None:
        self.root = root / "dashboards"
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid dashboard origin")
        self.origin = origin.rstrip("/")

    def save(self, profile_id: UUID, kind: str, dashboard_id: int) -> None:
        if kind not in {"labs", "whoop"} or dashboard_id < 1:
            raise ValueError("invalid dashboard destination")
        current = self.load(profile_id)
        payload = {"profile_id": str(profile_id), "origin": self.origin, **current}
        payload[kind] = dashboard_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=".destination-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.root / f"{profile_id}.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self, profile_id: UUID) -> dict[str, int]:
        path = self.root / f"{profile_id}.json"
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                value.get("profile_id") != str(profile_id)
                or value.get("origin") != self.origin
            ):
                return {}
            return {
                key: item
                for key in ("labs", "whoop")
                if isinstance((item := value.get(key)), int) and item > 0
            }
        except (OSError, ValueError, AttributeError):
            return {}
