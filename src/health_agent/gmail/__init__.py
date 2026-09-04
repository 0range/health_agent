"""Read-only Gmail medical attachment connector."""

from health_agent.gmail.config import GMAIL_READONLY_SCOPE, GmailAccount, GmailProfile
from health_agent.gmail.service import GmailService

__all__ = ["GMAIL_READONLY_SCOPE", "GmailAccount", "GmailProfile", "GmailService"]
