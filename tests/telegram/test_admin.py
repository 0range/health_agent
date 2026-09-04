from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from health_agent.telegram.admin import TelegramAdminService, TelegramIdentityConflict
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState


@dataclass
class FakeProfiles:
    ids: set[UUID]

    def exists(self, profile_id: UUID) -> bool:
        return profile_id in self.ids


def test_admin_validates_profile_and_prevents_cross_profile_binding(tmp_path) -> None:
    first = uuid4()
    second = uuid4()
    service = TelegramAdminService(
        PrivateBotTokenStore(tmp_path / "bot-token"),
        SqliteTelegramState(tmp_path / "state.sqlite3"),
        FakeProfiles({first, second}),
    )

    service.configure_token("123:secret")
    service.bind_identity(first, 101)
    with pytest.raises(TelegramIdentityConflict):
        service.bind_identity(second, 101)
    with pytest.raises(TelegramIdentityConflict):
        service.bind_identity(first, 202)
    with pytest.raises(ValueError, match="profile does not exist"):
        service.bind_identity(uuid4(), 303)

    assert service.status(first).token_configured
    assert service.status(first).identity_bound
    assert not service.status(second).identity_bound
    assert service.unbind_identity(first)
    assert not service.status(first).identity_bound
