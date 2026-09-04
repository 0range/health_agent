from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner

from health_agent import cli
from health_agent.importer import ImportReport


def test_import_output_contains_only_safe_counts(monkeypatch, tmp_path: Path) -> None:
    class FakeSettings:
        vault_root = tmp_path / "vault"

    @contextmanager
    def fake_session_scope(_engine: object):
        yield object()

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "build_engine", lambda _settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli, "FileVault", lambda _root: object())
    monkeypatch.setattr(
        cli,
        "import_document",
        lambda _session, _vault, _path, _source_uri: ImportReport(
            status="imported",
            document_id=UUID("00000000-0000-0000-0000-000000000001"),
            candidate_count=1,
            review_count=1,
        ),
    )

    result = CliRunner().invoke(cli.app, ["import-file", str(tmp_path / "labs.pdf")])

    assert result.exit_code == 0
    assert result.stdout == (
        "status=imported document_id=00000000-0000-0000-0000-000000000001 "
        "candidates=1 review_items=1\n"
    )


def test_review_commands_are_registered() -> None:
    result = CliRunner().invoke(cli.app, ["review", "--help"])

    assert result.exit_code == 0
    assert "list" in result.stdout
    assert "approve" in result.stdout
    assert "reject" in result.stdout
