from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.gmail.config import GmailAccount, GmailProfile
from health_agent.gmail.stores import LocalGmailProfileStore, LocalGmailStateStore
from health_agent.models import Profile
from health_agent.panel.models import ConnectorCard, ProfilePanel, ProfileSummary
from health_agent.panel.service import (
    GmailStatusReader,
    PanelService,
    ProfileNotFoundError,
    SqlAlchemyProfileRepository,
    TelegramStatusReader,
    _local_telegram_status,
    _whoop_card,
    build_panel_service,
)
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import (
    TelegramIdentity,
    TelegramStatus,
    VerifiedBotCredential,
)
from health_agent.whoop.status import WhoopStatus


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


def test_sqlalchemy_profiles_are_serialized_before_session_scope_closes(
    clean_database: Engine,
) -> None:
    profile_id = uuid4()
    profile = Profile(id=profile_id, name="Second profile")
    with session_scope(clean_database) as database_session:
        database_session.add(profile)
    profiles = SqlAlchemyProfileRepository(lambda: session_scope(clean_database))

    summaries = profiles.list()

    assert [(summary.id, summary.name) for summary in summaries] == [
        (UUID("00000000-0000-0000-0000-000000000001"), "Default"),
        (profile_id, "Second profile"),
    ]


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
    assert first_card.account_ids == second_card.account_ids == ("primary",)


def test_production_panel_construction_does_not_create_telegram_state(tmp_path) -> None:
    state_path = tmp_path / "telegram" / "state.sqlite3"
    settings = Settings(
        database_url="postgresql+psycopg://health-agent@127.0.0.1/test",
        gmail_root=tmp_path / "gmail",
        whoop_token_root=tmp_path / "whoop",
        telegram_token_file=tmp_path / "telegram" / "bot-token",
        telegram_state_path=state_path,
    )

    build_panel_service(settings)

    assert state_path.exists() is False
    assert state_path.parent.exists() is False


def test_local_telegram_status_is_scoped_to_the_requested_profile(tmp_path) -> None:
    first = uuid4()
    second = uuid4()
    tokens = PrivateBotTokenStore(tmp_path / "bot-token")
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    credential = VerifiedBotCredential("123:test-token", 123, "safe_bot")
    tokens.save_verified(credential)
    state.register_bot(credential.bot_id, credential.username)
    state.bind_identity(credential.bot_id, TelegramIdentity(111, first, 111))
    state.record_poll(credential.bot_id, "access_token=leaked MRI-result.pdf")

    first_status = _local_telegram_status(tokens, lambda: state, first)
    second_status = _local_telegram_status(tokens, lambda: state, second)
    reader = TelegramStatusReader(
        lambda profile_id: _local_telegram_status(tokens, lambda: state, profile_id)
    )
    first_card = reader.cards(first)[0]
    second_card = reader.cards(second)[0]

    assert first_status.identity_bound is True
    assert second_status.identity_bound is False
    assert first_status.delivery_unknown_count == second_status.delivery_unknown_count == 0
    assert first_card.last_success_at is second_card.last_success_at is None
    assert first_card.error_code is second_card.error_code is None


def test_persisted_connector_error_values_are_mapped_to_closed_safe_sets(tmp_path) -> None:
    profile_id = uuid4()
    unsafe_error = "refresh_token=secret MRI-result.pdf hemoglobin=12"
    whoop = _whoop_card(
        (
            WhoopStatus(
                configured=True,
                auth_status="connected",
                token_status="ready",
                last_success_at=None,
                retry_at=None,
                last_error_code=unsafe_error,
                weight_available=False,
                cycle_count=0,
                recovery_count=0,
                sleep_count=0,
                workout_count=0,
            ),
        )
    )
    gmail_profiles = LocalGmailProfileStore(tmp_path / "gmail")
    gmail_state = LocalGmailStateStore(tmp_path / "gmail")
    account = GmailAccount.create("primary")
    gmail_profiles.save(GmailProfile.empty(profile_id).upsert_account(account))
    gmail_state.fail_sync(str(profile_id), account.account_id, unsafe_error)
    gmail = GmailStatusReader(
        gmail_profiles, gmail_state, lambda _profile_id, _account_id: "valid"
    ).cards(profile_id)[0]
    telegram = TelegramStatusReader(
        lambda selected_profile_id: TelegramStatus(
            token_configured=True,
            credential_verified=True,
            bot_id=123,
            bot_username="safe_bot",
            webhook_configured=None,
            poller_running=False,
            delivery_unknown_count=0,
            profile_id=selected_profile_id,
            identity_bound=True,
            next_offset=None,
            last_poll_at=None,
            last_error_code=unsafe_error,
        )
    ).cards(profile_id)[0]

    payload = json.dumps(
        ProfilePanel(ProfileSummary(profile_id, "Person"), (whoop, gmail, telegram)).to_dict()
    )

    assert [whoop.error_code, gmail.error_code, telegram.error_code] == [
        "whoop_status_error",
        "gmail_status_error",
        "telegram_status_error",
    ]
    assert unsafe_error not in payload
    assert "secret" not in payload
    assert "MRI-result.pdf" not in payload
    assert "hemoglobin=12" not in payload


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
        detail="Локальный статус коннектора недоступен.",
        last_success_at=None,
        error_code="local_status_unavailable",
        account_ids=(),
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


def test_closed_connector_details_are_russian_ui_copy() -> None:
    profile_id = uuid4()
    profile = ProfileSummary(profile_id, "Person")
    telegram = TelegramStatusReader(
        lambda selected_profile_id: TelegramStatus(
            token_configured=False,
            credential_verified=False,
            bot_id=None,
            bot_username=None,
            webhook_configured=None,
            poller_running=False,
            delivery_unknown_count=0,
            profile_id=selected_profile_id,
            identity_bound=False,
            next_offset=None,
            last_poll_at=None,
            last_error_code="token_not_configured",
        )
    ).cards(profile_id)[0]
    service = service_with(
        FakeProfiles({profile.id: profile}),
        FakeReader("broken", lambda _profile_id: (_ for _ in ()).throw(OSError())),
    )

    assert telegram.detail == "Telegram не настроен локально."
    assert service.profile(profile_id).connectors[0].detail == "Локальный статус коннектора недоступен."
    assert service.profile(profile_id).connectors[-1].detail == (
        "Google Drive не интегрирован в этой установке."
    )
