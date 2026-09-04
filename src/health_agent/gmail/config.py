"""Multi-profile and multi-account Gmail connector configuration."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any
from uuid import UUID

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def normalize_profile_id(value: str | UUID) -> str:
    return str(UUID(str(value)))


def validate_account_id(value: str) -> str:
    if _ACCOUNT_ID.fullmatch(value) is None:
        raise ValueError(
            "account ID must be 1-64 letters, digits, underscores, or hyphens"
        )
    return value


def normalize_sender(value: str) -> str:
    sender = value.strip().casefold()
    if "@" not in sender or any(character.isspace() for character in sender):
        raise ValueError("trusted sender must be an email address")
    return sender


@dataclass(frozen=True, slots=True)
class GmailAccount:
    account_id: str
    email: str | None = None
    initial_lookback_days: int = 7
    trusted_senders: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        account_id: str,
        *,
        initial_lookback_days: int = 7,
        trusted_senders: tuple[str, ...] | list[str] = (),
    ) -> GmailAccount:
        if not 1 <= initial_lookback_days <= 365:
            raise ValueError("initial lookback must be between 1 and 365 days")
        return cls(
            validate_account_id(account_id),
            None,
            initial_lookback_days,
            tuple(dict.fromkeys(normalize_sender(value) for value in trusted_senders)),
        )

    def with_email(self, email: str) -> GmailAccount:
        return replace(self, email=normalize_sender(email))


@dataclass(frozen=True, slots=True)
class GmailProfile:
    profile_id: str
    accounts: tuple[GmailAccount, ...]

    @classmethod
    def empty(cls, profile_id: str | UUID) -> GmailProfile:
        return cls(normalize_profile_id(profile_id), ())

    def upsert_account(self, account: GmailAccount) -> GmailProfile:
        accounts = tuple(
            existing for existing in self.accounts if existing.account_id != account.account_id
        )
        return GmailProfile(self.profile_id, (*accounts, account))

    def account(self, account_id: str) -> GmailAccount:
        account_id = validate_account_id(account_id)
        for account in self.accounts:
            if account.account_id == account_id:
                return account
        raise KeyError(f"Gmail account {account_id!r} is not configured")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "accounts": [asdict(account) for account in self.accounts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GmailProfile:
        profile = cls.empty(str(data["profile_id"]))
        raw_accounts = data.get("accounts")
        if not isinstance(raw_accounts, list):
            raise TypeError("accounts must be a list")
        for raw in raw_accounts:
            if not isinstance(raw, dict):
                raise TypeError("each Gmail account must be an object")
            account = GmailAccount.create(
                str(raw["account_id"]),
                initial_lookback_days=int(raw.get("initial_lookback_days", 7)),
                trusted_senders=[str(value) for value in raw.get("trusted_senders", ())],
            )
            if raw.get("email") is not None:
                account = account.with_email(str(raw["email"]))
            profile = profile.upsert_account(account)
        return profile
