import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from health_agent.db import session_scope
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)
from health_agent.panel.http import PanelApplication
from health_agent.panel.service import PanelService, SqlAlchemyProfileRepository
from health_agent.panel.workflows import DatabaseWorkflowAdapter, WorkflowSnapshot

PROFILE = UUID("00000000-0000-0000-0000-000000000001")


def _service(engine):
    sessions = lambda: session_scope(engine)
    return PanelService(
        SqlAlchemyProfileRepository(sessions),
        workflows=DatabaseWorkflowAdapter(sessions),
    )


def _post(app, profile_id, fields, *, origin="http://127.0.0.1:8766"):
    return app.handle(
        "POST",
        f"/profiles/{profile_id}/medical",
        {"Host": "127.0.0.1:8766", "Origin": origin},
        urlencode({"csrf_token": "csrf", **fields}).encode(),
    )


def test_medical_panel_is_secure_escaped_and_replay_safe(clean_database):
    service = _service(clean_database)
    app = PanelApplication(service, csrf_token="csrf")
    path = f"/profiles/{PROFILE}/medical"
    page = app.handle("GET", path, {"Host": "127.0.0.1:8766"}, b"")
    assert page.status == 200 and b"Europe/Moscow" in page.body

    fields = {
        "operation": "visit_create",
        "action_id": "stable",
        "title": "Doctor <script>",
        "when": "2026-10-01T10:00",
    }
    assert _post(app, PROFILE, fields).status == 200
    assert _post(app, PROFILE, fields).status == 200
    snapshot = service.workflow_snapshot(PROFILE)
    assert len(snapshot.visits) == 1
    code = snapshot.visits[0].public_code
    assert (
        _post(
            app,
            PROFILE,
            {
                "operation": "visit_question",
                "action_id": "note-stable",
                "code": code,
                "text": "Ask <b>carefully</b>",
            },
        ).status
        == 200
    )
    assert (
        b"&lt;script&gt;"
        in app.handle("GET", path, {"Host": "127.0.0.1:8766"}, b"").body
    )
    assert (
        b"Ask &lt;b&gt;carefully&lt;/b&gt;"
        in app.handle("GET", path, {"Host": "127.0.0.1:8766"}, b"").body
    )
    assert _post(app, PROFILE, {**fields, "title": "Altered"}).status == 400
    assert _post(app, PROFILE, fields, origin="https://evil.test").status == 403
    assert (
        app.handle(
            "POST", path, {"Host": "evil.test", "Origin": "http://127.0.0.1:8766"}, b""
        ).status
        == 400
    )


def test_empty_medical_panel_has_only_creation_forms(clean_database):
    app = PanelApplication(_service(clean_database), csrf_token="csrf")

    response = app.handle(
        "GET",
        f"/profiles/{PROFILE}/medical",
        {"Host": "127.0.0.1:8766"},
        b"",
    )
    html = response.body.decode()

    assert response.status == 200
    assert html.count("<form ") == 2
    assert 'name="code"' not in html
    assert "Время — Москва" in html
    assert "Действия с визитами" not in html
    assert "Действия с напоминаниями" not in html


def test_medical_actions_are_collapsed_filtered_and_accessibly_labelled(
    clean_database,
):
    service = _service(clean_database)
    app = PanelApplication(service, csrf_token="csrf")
    for action_id, title in (("keep", "Будущий приём"), ("cancel", "Отменённый")):
        assert (
            _post(
                app,
                PROFILE,
                {
                    "operation": "visit_create",
                    "action_id": action_id,
                    "title": title,
                    "when": "2026-10-01T10:00",
                },
            ).status
            == 200
        )
    visits = service.workflow_snapshot(PROFILE).visits
    cancelled = next(item for item in visits if item.title == "Отменённый")
    assert (
        _post(
            app,
            PROFILE,
            {
                "operation": "visit_cancel",
                "action_id": "cancel-visit",
                "code": cancelled.public_code,
            },
        ).status
        == 200
    )
    assert (
        _post(
            app,
            PROFILE,
            {
                "operation": "reminder_create",
                "action_id": "reminder",
                "title": "Проверить назначение",
                "when": "2026-10-02T11:00",
                "repeat_unit": "",
                "repeat_every": "",
            },
        ).status
        == 200
    )

    html = app.handle(
        "GET",
        f"/profiles/{PROFILE}/medical",
        {"Host": "127.0.0.1:8766"},
        b"",
    ).body.decode()

    assert '<details class="medical-actions">' in html
    assert "Действия с визитами" in html
    assert "Действия с напоминаниями" in html
    assert "Будущий приём — 01.10.2026 10:00" in html
    assert f'value="{cancelled.public_code}"' not in html
    assert 'value="reminder_done"' not in html
    assert 'value="reminder_confirm"' in html
    ids = re.findall(r'\sid="([^"]+)"', html)
    label_targets = re.findall(r'<label for="([^"]+)">', html)
    assert len(ids) == len(set(ids))
    assert set(label_targets).issubset(ids)


def test_calendar_connection_translates_only_known_machine_prefix(clean_database):
    class Stub:
        def workflow_snapshot(self, _profile):
            return WorkflowSnapshot((), (), (), calendar_connection="oauth_required")

    app = PanelApplication(Stub(), csrf_token="csrf")  # type: ignore[arg-type]
    html = app.handle(
        "GET",
        f"/profiles/{PROFILE}/medical",
        {"Host": "127.0.0.1:8766"},
        b"",
    ).body.decode()

    assert "Требуется подключить Calendar." in html
    assert "oauth_required" not in html


def test_unknown_and_foreign_profile_cannot_mutate(clean_database):
    other, missing = uuid4(), uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Other"))
    service = _service(clean_database)
    app = PanelApplication(service, csrf_token="csrf")
    fields = {
        "operation": "visit_create",
        "action_id": "one",
        "title": "A",
        "when": "2026-10-01T10:00",
    }
    assert _post(app, missing, fields).status == 404
    assert _post(app, PROFILE, fields).status == 200
    code = service.workflow_snapshot(PROFILE).visits[0].public_code
    assert (
        _post(
            app, other, {"operation": "visit_done", "action_id": "two", "code": code}
        ).status
        == 400
    )
    assert service.workflow_snapshot(PROFILE).visits[0].status == "planned"


def test_reminder_replay_completion_and_csrf(clean_database):
    service, app = (
        _service(clean_database),
        PanelApplication(_service(clean_database), csrf_token="csrf"),
    )
    fields = {
        "operation": "reminder_create",
        "action_id": "repeat",
        "title": "Check",
        "when": "2026-10-01T10:00",
        "repeat_unit": "months",
        "repeat_every": "1",
    }
    assert (
        _post(app, PROFILE, fields).status == _post(app, PROFILE, fields).status == 200
    )
    reminder = service.workflow_snapshot(PROFILE).reminders[0]
    assert len(service.workflow_snapshot(PROFILE).reminders) == 1
    assert (
        _post(
            app,
            PROFILE,
            {
                "operation": "reminder_confirm",
                "action_id": "confirm",
                "code": reminder.public_code,
            },
        ).status
        == 200
    )
    assert (
        _post(
            app,
            PROFILE,
            {
                "operation": "reminder_done",
                "action_id": "done",
                "code": reminder.public_code,
            },
        ).status
        == 200
    )
    assert len(service.workflow_snapshot(PROFILE).reminders) == 1
    before = len(service.workflow_snapshot(PROFILE).visits)
    denied = app.handle(
        "POST",
        f"/profiles/{PROFILE}/medical",
        {"Host": "127.0.0.1:8766", "Origin": "http://127.0.0.1:8766"},
        urlencode(
            {
                "csrf_token": "bad",
                "operation": "visit_create",
                "action_id": "bad",
                "title": "No",
                "when": "2026-10-01T10:00",
            }
        ).encode(),
    )
    assert (
        denied.status == 403
        and len(service.workflow_snapshot(PROFILE).visits) == before
    )


def test_prepare_renders_verified_owner_sources_only_and_get_is_read_only(
    clean_database,
):
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Other"))
        session.flush()
        for profile, value, status in (
            (PROFILE, "95", ReviewStatus.VERIFIED),
            (PROFILE, "777", ReviewStatus.NEEDS_REVIEW),
            (other, "888", ReviewStatus.VERIFIED),
        ):
            doc = Document(
                profile_id=profile,
                sha256=uuid4().hex * 2,
                vault_path="/private/not-output",
                media_type="application/pdf",
                document_type="lab",
                collected_date=date(2026, 9, 1),
            )
            session.add(doc)
            session.flush()
            session.add(
                DocumentPage(
                    document_id=doc.id, page_number=1, extraction_method="test"
                )
            )
            session.flush()
            session.add(
                LabObservation(
                    document_id=doc.id,
                    page_number=1,
                    canonical_name="glucose",
                    source_name="Glucose",
                    source_value=value,
                    parsed_value=Decimal(value),
                    source_unit="mg/dL",
                    normalized_value=Decimal(value),
                    normalized_unit="mg/dL",
                    evidence_excerpt="test",
                    confidence=1,
                    status=status,
                )
            )
            session.flush()
    service, app = (
        _service(clean_database),
        PanelApplication(_service(clean_database), csrf_token="csrf"),
    )
    _post(
        app,
        PROFILE,
        {
            "operation": "visit_create",
            "action_id": "visit",
            "title": "Doctor",
            "when": "2026-10-01T10:00",
        },
    )
    code = service.workflow_snapshot(PROFILE).visits[0].public_code
    assert service.workflow_snapshot(PROFILE).notes[0][1] == ()
    app.handle("GET", f"/profiles/{PROFILE}/medical", {"Host": "127.0.0.1:8766"}, b"")
    assert service.workflow_snapshot(PROFILE).notes[0][1] == ()
    response = _post(
        app,
        PROFILE,
        {"operation": "visit_prepare", "action_id": "prepare", "code": code},
    )
    assert (
        response.status == 200
        and b"95" in response.body
        and b"document:" in response.body
    )
    assert b"777" not in response.body and b"888" not in response.body
    assert "исключённых из подготовки: 1".encode() in response.body


def test_medical_routes_hide_database_failures():
    sentinel = "private-sql-sentinel"

    class Broken:
        def workflow_snapshot(self, _profile):
            raise SQLAlchemyError(sentinel)

        def workflow_action(self, _profile, _fields):
            raise SQLAlchemyError(sentinel)

    app = PanelApplication(Broken(), csrf_token="csrf")  # type: ignore[arg-type]
    get = app.handle(
        "GET", f"/profiles/{PROFILE}/medical", {"Host": "127.0.0.1:8766"}, b""
    )
    post = _post(
        app,
        PROFILE,
        {"operation": "visit_done", "action_id": "x", "code": "visit-code"},
    )
    assert (
        get.status == post.status == 503
        and sentinel.encode() not in get.body + post.body
    )


def test_duplicate_fields_and_unknown_operations_are_rejected(clean_database):
    app = PanelApplication(_service(clean_database), csrf_token="csrf")
    path = f"/profiles/{PROFILE}/medical"
    headers = {"Host": "127.0.0.1:8766", "Origin": "http://127.0.0.1:8766"}
    duplicate = app.handle(
        "POST",
        path,
        headers,
        b"csrf_token=csrf&operation=visit_done&operation=visit_cancel&action_id=x&code=visit-code",
    )
    unknown = _post(app, PROFILE, {"operation": "erase_everything", "action_id": "x"})
    assert duplicate.status == unknown.status == 400
