from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pymupdf
import pytest
from sqlalchemy import select

from health_agent.importer import import_document
from health_agent.models import LabObservation, Profile, ReviewStatus
from health_agent.questions.context import HealthContextBuilder
from health_agent.telegram.review import TelegramReviewActions
from health_agent.telegram.types import MessageContext
from health_agent.vault import FileVault

CONTEXT = MessageContext(1, UUID(int=1), 10, 10, 2, 3, None, datetime.now(UTC))


def candidate(
    session,
    tmp_path,
    *,
    profile_id=CONTEXT.profile_id,
    provider="telegram",
    external_id="telegram:1:10:1:file",
    value="42",
):
    path = tmp_path / f"{uuid4()}.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), f"Ferritin {value} ng/mL 30-400")
        pdf.save(path)
    report = import_document(
        session,
        FileVault(tmp_path / "vault"),
        path,
        None,
        profile_id=profile_id,
        source_provider=provider,
        source_external_id=external_id,
        collected_date=date(2026, 9, 4),
    )
    observation = session.scalar(
        select(LabObservation).where(LabObservation.document_id == report.document_id)
    )
    observation_id = observation.id
    session.commit()
    return observation_id


def test_review_one_item_and_explicit_confirmation(session, tmp_path):
    first = candidate(session, tmp_path)
    second = candidate(session, tmp_path, value="43", external_id="telegram:1:10:2:f")
    actions = TelegramReviewActions(session.get_bind())
    reply = actions.handle(CONTEXT, "/review")
    assert str(first) in reply and str(second) not in reply
    assert "42 ng/mL" in reply and "2026-09-04" in reply
    assert "Unverified" in reply and "page 1" in reply
    assert actions.handle(CONTEXT, "change my ferritin to 999") is None
    session.expire_all()
    assert session.get_one(LabObservation, first).status is ReviewStatus.NEEDS_REVIEW
    confirmed = actions.handle(CONTEXT, f"/confirm {first}")
    assert confirmed == f"Confirmed item {first}."
    session.expire_all()
    assert session.get_one(LabObservation, first).status is ReviewStatus.VERIFIED
    assert str(second) in actions.handle(CONTEXT, "/review")
    assert (
        TelegramReviewActions(session.get_bind()).handle(CONTEXT, f"/confirm {first}")
        == confirmed
    )
    assert "already resolved" in actions.handle(CONTEXT, f"/reject {first}")


def test_correction_preserves_lineage_and_exact_replay(session, tmp_path):
    original_id = candidate(session, tmp_path)
    actions = TelegramReviewActions(session.get_bind())
    command = f"/correct {original_id} 43,5 ng/mL"
    reply = actions.handle(CONTEXT, command)
    assert reply == f"Corrected item {original_id}."
    assert TelegramReviewActions(session.get_bind()).handle(CONTEXT, command) == reply
    assert "already resolved" in actions.handle(
        CONTEXT, f"/correct {original_id} 99 ng/mL"
    )
    session.expire_all()
    original = session.get_one(LabObservation, original_id)
    assert original.source_value == "42" and original.status is ReviewStatus.REJECTED
    corrected = session.scalars(
        select(LabObservation).where(
            LabObservation.supersedes_observation_id == original_id
        )
    ).one()
    assert corrected.normalized_value == Decimal("43.5")
    assert corrected.status is ReviewStatus.VERIFIED
    context = HealthContextBuilder(session).build(CONTEXT.profile_id, "ferritin")
    assert any("43.5" in str(fact) for fact in context.evidence)


@pytest.mark.parametrize("scope", ["profile", "chat", "bot", "provider"])
def test_review_cannot_read_or_mutate_other_scope(session, tmp_path, scope):
    profile = CONTEXT.profile_id
    external_id = "telegram:1:10:1:file"
    provider = "telegram"
    if scope == "profile":
        profile = uuid4()
        session.add(Profile(id=profile, name="Other synthetic profile"))
        session.commit()
    elif scope == "chat":
        external_id = "telegram:1:100:1:file"
    elif scope == "bot":
        external_id = "telegram:2:10:1:file"
    else:
        provider = "google_drive"
    item = candidate(
        session,
        tmp_path,
        profile_id=profile,
        provider=provider,
        external_id=external_id,
    )
    actions = TelegramReviewActions(session.get_bind())
    assert "No pending" in actions.handle(CONTEXT, "/review")
    assert "unavailable" in actions.handle(CONTEXT, f"/confirm {item}")
    session.expire_all()
    assert session.get_one(LabObservation, item).status is ReviewStatus.NEEDS_REVIEW


def test_reject_and_invalid_correction_are_safe(session, tmp_path):
    item = candidate(session, tmp_path)
    actions = TelegramReviewActions(session.get_bind())
    assert "not applied" in actions.handle(CONTEXT, f"/correct {item} NaN secret-token")
    session.expire_all()
    assert session.get_one(LabObservation, item).status is ReviewStatus.NEEDS_REVIEW
    reply = actions.handle(CONTEXT, f"/reject {item}")
    assert reply == f"Rejected item {item}."
    assert actions.handle(CONTEXT, f"/reject {item}") == reply
    assert "Usage" in actions.handle(CONTEXT, "/confirm")
    assert "Usage" in actions.handle(CONTEXT, "/review extra")
    assert "Usage" in actions.handle(CONTEXT, "/correct " + "x" * 1000)
    assert actions.handle(CONTEXT, "/other") is None


def test_review_failure_hides_database_details(tmp_path):
    class Unavailable:
        def connect(self):
            raise RuntimeError("private database and token")

    actions = TelegramReviewActions(Unavailable())
    assert actions.handle(CONTEXT, "/review") == "Review is temporarily unavailable."


def test_concurrent_correction_is_one_version(session, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    item_id = candidate(session, tmp_path)
    actions = TelegramReviewActions(session.get_bind())
    command = f"/correct {item_id} 43 ng/mL"
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(lambda _: actions.handle(CONTEXT, command), range(2))
        )
    assert results == [f"Corrected item {item_id}."] * 2
    session.expire_all()
    assert (
        len(
            session.scalars(
                select(LabObservation).where(
                    LabObservation.supersedes_observation_id == item_id
                )
            ).all()
        )
        == 1
    )
