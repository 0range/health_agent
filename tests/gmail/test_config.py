from __future__ import annotations

from uuid import UUID

import pytest

from health_agent.gmail.config import GmailAccount, GmailProfile

PROFILE_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_profile_supports_multiple_account_slots_and_updates_one() -> None:
    profile = GmailProfile.empty(PROFILE_ID)
    profile = profile.upsert_account(GmailAccount.create("personal"))
    profile = profile.upsert_account(
        GmailAccount.create("work", initial_lookback_days=14).with_email(
            "USER@EXAMPLE.COM"
        )
    )
    profile = profile.upsert_account(
        GmailAccount.create("personal", trusted_senders=["LAB@EXAMPLE.COM"])
    )

    assert [account.account_id for account in profile.accounts] == ["work", "personal"]
    assert profile.account("work").email == "user@example.com"
    assert profile.account("personal").trusted_senders == ("lab@example.com",)


@pytest.mark.parametrize("account_id", ("../gmail", "has space", ""))
def test_rejects_unsafe_account_id(account_id: str) -> None:
    with pytest.raises(ValueError):
        GmailAccount.create(account_id)


@pytest.mark.parametrize("days", (0, 366))
def test_rejects_unbounded_initial_lookback(days: int) -> None:
    with pytest.raises(ValueError):
        GmailAccount.create("personal", initial_lookback_days=days)


def test_profile_json_round_trip_preserves_account_binding() -> None:
    profile = GmailProfile.empty(PROFILE_ID).upsert_account(
        GmailAccount.create(
            "personal", initial_lookback_days=9, trusted_senders=["lab@example.com"]
        ).with_email("me@example.com")
    )
    assert GmailProfile.from_dict(profile.to_dict()) == profile
