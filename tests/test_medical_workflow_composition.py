from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.config import Settings
from health_agent.questions.composition import (
    NeedsAttentionMedicalInbox,
    QuestionStatus,
    build_telegram_question_runtime,
)
from health_agent.telegram.types import MessageContext, VerifiedBotCredential


def test_single_schema_head_and_visit_cli_help():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_heads() == [
        "0014_v01_workflow_evidence"
    ]
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "visit" in result.stdout


def test_composed_telegram_routes_visits_and_leaves_questions_for_responder(
    clean_database, tmp_path: Path
):
    captured = {}

    class Tokens:
        def __init__(self, _path):
            pass

        def load_verified(self):
            return VerifiedBotCredential("safe", 7, "bot")

    class State:
        def register_bot(self, *_args):
            pass

    def updates(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    build_telegram_question_runtime(
        Settings(
            telegram_root=tmp_path / "telegram",
            telegram_token_file=tmp_path / "token",
            telegram_state_path=tmp_path / "state.sqlite",
            telegram_staging_path=tmp_path / "staging",
        ),
        question_application_factory=lambda _: SimpleNamespace(),
        token_store_factory=Tokens,
        state_factory=lambda _: State(),
        gateway_factory=lambda _: SimpleNamespace(),
        messenger_factory=lambda *_: SimpleNamespace(),
        update_service_factory=updates,
        poller_factory=lambda *_: SimpleNamespace(run_forever=lambda: None),
        medical_inbox=NeedsAttentionMedicalInbox(),
        status_reader=lambda _: QuestionStatus(True, {}),
        engine_factory=lambda _: clean_database,
    )
    actions = captured["text_actions"]
    context = MessageContext(
        7,
        UUID("00000000-0000-0000-0000-000000000001"),
        1,
        1,
        1,
        10,
        None,
        datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert "Визиты" in actions.handle(context, "/visits")
    assert actions.handle(replace(context, update_id=11), "Как я спал?") is None
