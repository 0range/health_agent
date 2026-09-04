from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.stores import LocalGmailProfileStore, LocalGmailStateStore
from health_agent.panel.models import ConnectorCard, ProfilePanel, ProfileSummary
from health_agent.panel.service import (
    GmailStatusReader,
    PanelService,
    ProfileNotFoundError,
    _local_telegram_status,
)
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import TelegramIdentity, VerifiedBotCredential


@dataclass
class FakeProfiles:
    values: dict[UUID, ProfileSummary]

    def list(self) -> tuple[ProfileSummary, ...]:
        return tuple(sorted(self.values.values(), key=lambda profile: profile.name))

    def get(self, profile_id: UUID) -> ProfileSummary | None:
        return self.values.get(profile_id)

    def create(self, name: str) -> ProfileSummary:
        profile = ProfileSummary(id=uuid4(), name=name)
        self.values[profile.id] = profile
        return profile


class FakeReader:
    def __init__(self, connector: str, cards: Callable[[UUID], tuple[ConnectorCard, ...]]) -> None:
        self.connector = connector
        self._cards = cards

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        return self._cards(profile_id)


def card(connector: str, status: str, *, error_code: str | None = None) -> ConnectorCard:
    return ConnectorCard(
        connector=connector,
        status=status,
        detail="safe status",
        last_success_at=datetime(2026, 9, 4, tzinfo=UTC),
        error_code=error_code,
    )


def service_with(
    profiles: FakeProfiles, *readers: FakeReader
) -> PanelService:
    return PanelService(profiles, readers)


def test_lists_profiles_by_repository_order_and_creates_a_profile() -> None:
    beta = ProfileSummary(uuid4(), "Beta")
    alpha = ProfileSummary(uuid4(), "Alpha")
    profiles = FakeProfiles({beta.id: beta, alpha.id: alpha})
    service = service_with(profiles)

    assert service.list_profiles() == (alpha, beta)

    created = service.create_profile("  New profile  ")

    assert created.name == "New profile"
    assert service.list_profiles() == (alpha, beta, created)


def test_rejects_a_missing_profile() -> None:
    service = service_with(FakeProfiles({}))

    with pytest.raises(ProfileNotFoundError):
        service.profile(uuid4())


def test_profile_connector_statuses_are_isolated() -> None:
    first = ProfileSummary(uuid4(), "First")
    second = ProfileSummary(uuid4(), "Second")
    profiles = FakeProfiles({first.id: first, second.id: second})
    whoop = FakeReader(
        "whoop",
        lambda profile_id: (card("whoop", "ready"),)
        if profile_id == first.id
        else (card("whoop", "not_connected"),),
    )
    gmail = FakeReader(
        "gmail",
        lambda profile_id: (card("gmail", "needs_authorization"),)
        if profile_id == first.id
        else (card("gmail", "ready"),),
    )
    telegram = FakeReader(
        "telegram",
        lambda profile_id: (card("telegram", "bound"),)
        if profile_id == first.id
        else (card("telegram", "not_configured"),),
    )
    service = service_with(profiles, whoop, gmail, telegram)

    first_panel = service.profile(first.id)
    second_panel = service.profile(second.id)

    assert [(item.connector, item.status) for item in first_panel.connectors] == [
        ("whoop", "ready"),
        ("gmail", "needs_authorization"),
        ("telegram", "bound"),
        ("drive", "not_available"),
    ]
    assert [(item.connector, item.status) for item in second_panel.connectors] == [
        ("whoop", "not_connected"),
        ("gmail", "ready"),
        ("telegram", "not_configured"),
        ("drive", "not_available"),
    ]


def test_gmail_reader_uses_only_the_selected_profile_directory(tmp_path) -> None:
    first = uuid4()
    second = uuid4()
    profiles = LocalGmailProfileStore(tmp_path)
    state = LocalGmailStateStore(tmp_path)
    account = GmailAccount.create("primary")
    profiles.save(GmailProfile.empty(first).upsert_account(account))
    profiles.save(GmailProfile.empty(second).upsert_account(account))
    state.finish_sync(str(first), account.account_id)
    reader = GmailStatusReader(
        profiles,
        state,
        lambda profile_id, _account_id: "valid" if profile_id == str(first) else "missing",
    )

    first_card = reader.cards(first)[0]
    second_card = reader.cards(second)[0]

    assert first_card.status == "ready"
    assert first_card.last_success_at is not None
    assert second_card.status == "needs_authorization"
    assert second_card.last_success_at is None


def test_local_telegram_status_is_scoped_to_the_requested_profile(tmp_path) -> None:
    first = uuid4()
    second = uuid4()
    tokens = PrivateBotTokenStore(tmp_path / "bot-token")
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    credential = VerifiedBotCredential("123:test-token", 123, "safe_bot")
    tokens.save_verified(credential)
    state.register_bot(credential.bot_id, credential.username)
    state.bind_identity(credential.bot_id, TelegramIdentity(111, first, 111))

    first_status = _local_telegram_status(tokens, state, first)
    second_status = _local_telegram_status(tokens, state, second)

    assert first_status.identity_bound is True
    assert second_status.identity_bound is False
    assert first_status.delivery_unknown_count == second_status.delivery_unknown_count == 0


def test_one_unreadable_connector_becomes_a_safe_card() -> None:
    profile = ProfileSummary(uuid4(), "Person")
    broken = FakeReader(
        "gmail", lambda _profile_id: (_ for _ in ()).throw(OSError("token=leaked"))
    )
    service = service_with(FakeProfiles({profile.id: profile}), broken)

    panel = service.profile(profile.id)

    assert panel.connectors[0] == ConnectorCard(
        connector="gmail",
        status="status_unavailable",
        detail="Local connector status is unavailable.",
        last_success_at=None,
        error_code="local_status_unavailable",
    )
    assert panel.connectors[-1].status == "not_available"
    assert "token=leaked" not in json.dumps(panel.to_dict())


def test_serialized_view_models_contain_only_safe_display_fields() -> None:
    panel = ProfilePanel(
        profile=ProfileSummary(uuid4(), "Person"),
        connectors=(card("whoop", "ready"),),
    )

    payload = json.dumps(panel.to_dict())

    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert "medical" not in payload
    assert "source_value" not in payload
