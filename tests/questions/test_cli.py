from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from typer.testing import CliRunner

import health_agent.cli as cli_module
from health_agent.cli import app
from health_agent.questions.composition import QuestionStatus
from health_agent.questions.models import EvidenceSource
from health_agent.questions.service import QuestionAnswerErrorCode, QuestionAnswerResult

PROFILE_ID = "00000000-0000-0000-0000-000000000001"


class FakeQuestionService:
    def __init__(self, result: QuestionAnswerResult) -> None:
        self.result = result
        self.calls: list[tuple[UUID, str]] = []

    def answer(self, profile_id: UUID, question: str) -> QuestionAnswerResult:
        self.calls.append((profile_id, question))
        return self.result


def test_question_ask_outputs_answer_without_echoing_question_or_secret(monkeypatch) -> None:
    secret = "sk-test-secret"
    question = "my private question"
    service = FakeQuestionService(QuestionAnswerResult("Safe answer [LAB1]", None))
    monkeypatch.setattr(cli_module, "build_question_application", lambda _: service)

    result = CliRunner().invoke(
        app, ["question", "ask", "--profile-id", PROFILE_ID, question]
    )

    assert result.exit_code == 0
    assert result.stdout == "Safe answer [LAB1]\n"
    assert service.calls == [(UUID(PROFILE_ID), question)]
    assert question not in result.stdout
    assert secret not in result.stdout


def test_question_ask_returns_a_safe_nonzero_error(monkeypatch) -> None:
    secret = "private-openai-key"
    monkeypatch.setattr(
        cli_module,
        "build_question_application",
        lambda _: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    result = CliRunner().invoke(
        app, ["question", "ask", "--profile-id", PROFILE_ID, "private question"]
    )

    assert result.exit_code == 1
    assert "temporarily unavailable" in result.output
    assert "private question" not in result.output
    assert secret not in result.output


def test_question_status_shows_only_counts_and_safe_error_codes(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "question_status",
        lambda _, __: QuestionStatus(True, {EvidenceSource.LAB: 3}),
    )

    result = CliRunner().invoke(app, ["question", "status", "--profile-id", PROFILE_ID])

    assert result.exit_code == 0
    assert "status=ready" in result.stdout
    assert "lab=3" in result.stdout
    assert "sk-" not in result.stdout
    assert "Ferritin" not in result.stdout


def test_telegram_run_handles_interrupt_and_never_prints_credential(monkeypatch) -> None:
    token = "123:telegram-secret"

    class Poller:
        def run_forever(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        cli_module,
        "build_telegram_question_runtime",
        lambda _: SimpleNamespace(poller=Poller()),
    )

    result = CliRunner().invoke(app, ["telegram", "run"])

    assert result.exit_code == 0
    assert result.stdout == "status=running\nstatus=stopped\n"
    assert token not in result.output


def test_question_result_error_exits_without_exposing_request(monkeypatch) -> None:
    service = FakeQuestionService(
        QuestionAnswerResult(
            "Health-question answering is temporarily unavailable.",
            QuestionAnswerErrorCode.CONTEXT_UNAVAILABLE,
        )
    )
    monkeypatch.setattr(cli_module, "build_question_application", lambda _: service)

    result = CliRunner().invoke(
        app, ["question", "ask", "--profile-id", PROFILE_ID, "do not echo me"]
    )

    assert result.exit_code == 1
    assert "temporarily unavailable" in result.output
    assert "do not echo me" not in result.output
