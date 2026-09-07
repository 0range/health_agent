import json
from copy import deepcopy
from uuid import uuid4

import pytest

from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionProfile
from health_agent.lab_extraction.openai import OpenAILabExtractor
from health_agent.lab_extraction.types import ExtractionError
from health_agent.lab_extraction.validation import validate_candidates
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from lab_extraction.test_openai import Client, FakeSettings
from lab_extraction.test_service import add_page, service

TEXT = "Assay monomeric (post\nPEG)\n137\nmIU/L\n72 - 229"
BODY = {
    "candidates": [
        {
            "source_name": "Assay monomeric (post PEG)",
            "source_value": "137",
            "source_unit": "mIU/L",
            "reference_text": "72 - 229",
            "source_flag": None,
            "evidence_excerpt": TEXT,
        }
    ]
}


def extract(body, text=TEXT):
    return OpenAILabExtractor(
        FakeSettings(), client=Client(text=json.dumps(body))
    ).extract(DEFAULT_PROFILE_ID, text)


def test_cloud_restores_only_exact_source_name_whitespace():
    with pytest.raises(ValueError):
        validate_candidates(BODY, TEXT)
    result = extract(BODY)[0]
    assert result.source_name == "Assay monomeric (post\nPEG)"
    assert result.source_value == "137" and result.source_unit == "mIU/L"
    assert result.reference_text == "72 - 229" and result.evidence_excerpt == TEXT
    exact = deepcopy(BODY)
    exact["candidates"][0]["source_name"] = result.source_name
    assert extract(exact) == (result,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_name", "Assay polymeric (post PEG)"),
        ("source_value", "138"),
        ("source_unit", "IU/L"),
        ("reference_text", "72-229"),
        ("evidence_excerpt", TEXT.replace("\n", " ")),
        ("evidence_excerpt", None),
    ],
)
def test_recovery_rejects_changed_evidence(field, value):
    body = deepcopy(BODY)
    body["candidates"][0][field] = value
    with pytest.raises(ExtractionError, match="cloud_invalid_output"):
        extract(body)


def test_recovery_rejects_foreign_and_ambiguous_evidence():
    for text in ("Other assay 137 mIU/L 72 - 229", TEXT + "\n" + TEXT):
        with pytest.raises(ExtractionError, match="cloud_invalid_output"):
            extract(BODY, text)


def test_budget_500_and_lowering_preserves_usage(clean_database, tmp_path):
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    assert worker.status(DEFAULT_PROFILE_ID).daily_budget == 20
    worker.configure(DEFAULT_PROFILE_ID, cloud=True, daily_budget=500)
    with session_scope(clean_database) as session:
        session.get_one(
            LabExtractionProfile, DEFAULT_PROFILE_ID
        ).cloud_requests_today = 123
    worker.configure(DEFAULT_PROFILE_ID, daily_budget=20)
    with session_scope(clean_database) as session:
        assert (
            session.get_one(
                LabExtractionProfile, DEFAULT_PROFILE_ID
            ).cloud_requests_today
            == 123
        )
    with pytest.raises(ExtractionError, match="invalid_daily_budget"):
        worker.configure(DEFAULT_PROFILE_ID, daily_budget=501)


def test_targeted_run_is_document_and_profile_scoped(clean_database, tmp_path):
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Synthetic"))
    foreign = add_page(clean_database, profile_id=other)
    target = add_page(clean_database)
    add_page(clean_database)
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID)
    for missing in (foreign, uuid4()):
        assert worker.run(DEFAULT_PROFILE_ID, document_id=missing).processed == 0
    assert worker.run(DEFAULT_PROFILE_ID, document_id=target).processed == 1
    assert worker.run(DEFAULT_PROFILE_ID, document_id=target).processed == 0
    assert worker.run(DEFAULT_PROFILE_ID).processed == 1


def test_yandex_restoration_is_persisted_review_only(clean_database, tmp_path):
    from ai.test_yandex import RecordingCompletions, chat_response, client_for, settings
    from sqlalchemy import select

    from health_agent.ai.yandex import YandexLabExtractor
    from health_agent.lab_extraction.models import LabExtractionJob
    from health_agent.models import LabObservation, ReviewStatus

    adapter = YandexLabExtractor(
        settings(yandex_allowed_profile_ids=(DEFAULT_PROFILE_ID,)),
        client=client_for(RecordingCompletions(chat_response(json.dumps(BODY)))),
    )
    target = add_page(clean_database, TEXT + "\nUnknown marker 2 U/L")
    worker = service(clean_database, tmp_path, cloud=adapter)
    worker.configure(DEFAULT_PROFILE_ID, cloud=True)
    report = worker.run(DEFAULT_PROFILE_ID, document_id=target)
    assert report.cloud_requests == 1 and report.inserted == 1
    with session_scope(clean_database) as session:
        row = session.scalars(select(LabObservation)).one()
        assert row.source_name == "Assay monomeric (post\nPEG)"
        assert row.evidence_excerpt == TEXT
        assert row.status == ReviewStatus.NEEDS_REVIEW
        job = session.scalars(select(LabExtractionJob)).one()
        assert job.extractor_version == "lab-extraction-v1"
        assert job.cloud_attempts == 1
    assert worker.run(DEFAULT_PROFILE_ID, document_id=target).processed == 0


def test_budget_migration_preserves_data_and_refuses_unsafe_down(
    clean_database, tmp_path
):
    from alembic.config import Config
    from sqlalchemy.exc import DBAPIError

    from alembic import command

    config = Config("alembic.ini")
    worker = service(clean_database, tmp_path)
    worker.configure(DEFAULT_PROFILE_ID, cloud=True, daily_budget=100)
    with session_scope(clean_database) as session:
        session.get_one(
            LabExtractionProfile, DEFAULT_PROFILE_ID
        ).cloud_requests_today = 87
    with clean_database.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0015_pilot_records")
        command.upgrade(config, "head")
    with session_scope(clean_database) as session:
        row = session.get_one(LabExtractionProfile, DEFAULT_PROFILE_ID)
        assert row.daily_budget == 100 and row.cloud_requests_today == 87
        assert row.cloud_enabled
    worker.configure(DEFAULT_PROFILE_ID, daily_budget=500)
    with (
        pytest.raises(
            DBAPIError, match="Refusing to downgrade configured extraction budget"
        ),
        clean_database.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "0015_pilot_records")


def test_cli_budget_and_document_filter(clean_database, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from health_agent.lab_extraction import cli

    worker = service(clean_database, tmp_path)
    monkeypatch.setattr(cli, "build_service", lambda: worker)
    runner = CliRunner()
    profile = str(DEFAULT_PROFILE_ID)
    assert (
        runner.invoke(
            cli.app, ["configure", profile, "--daily-budget", "500"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli.app, ["configure", profile, "--daily-budget", "501"]
        ).exit_code
        != 0
    )
    target = add_page(clean_database)
    add_page(clean_database)
    result = runner.invoke(cli.app, ["run", profile, "--document-id", str(target)])
    assert result.exit_code == 0 and "processed=1" in result.output
