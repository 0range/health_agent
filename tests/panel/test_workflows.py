from urllib.parse import urlencode
from uuid import UUID, uuid4

from health_agent.db import session_scope
from health_agent.models import Profile
from health_agent.panel.http import PanelApplication
from health_agent.panel.service import PanelService, SqlAlchemyProfileRepository
from health_agent.panel.workflows import DatabaseWorkflowAdapter

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
