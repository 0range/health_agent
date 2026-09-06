from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import DisposablePostgres
from sqlalchemy import Engine, insert, text
from sqlalchemy.orm import Session
from test_metabase import FakeMetabase

from health_agent import lab_dashboard
from health_agent.lab_dashboard import (
    DEFAULT_SERIES,
    LabSeries,
    bootstrap_lab_dashboard,
    discover_lab_series,
    lab_card_specs,
)
from health_agent.lab_extraction.registry import _ANALYTES
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)

PROFILE = UUID(int=1)
FERRITIN = LabSeries("ferritin", "Ферритин", "ng/mL")


@pytest.fixture
def db_session(session: Session) -> Session:
    return session


def test_specs_are_unit_specific_profile_bound_and_unaggregated() -> None:
    specs = lab_card_specs(PROFILE, (FERRITIN,))
    assert len(specs) == 2
    assert specs[0].display == "table"
    assert (
        lab_dashboard._visualization(specs[0])["column_settings"][
            '["name","comparison"]'
        ]["column_title"]
        == "Сравнение"
    )
    assert "1000" in specs[0].description
    assert "Ферритин" in specs[1].name and "ng/mL" in specs[1].name
    assert all(str(PROFILE) in spec.query for spec in specs)
    assert "LIMIT" not in specs[1].query and "GROUP BY" not in specs[1].query
    assert specs[1].metrics == ("result", "reference_low", "reference_high")
    assert "date_label" in specs[1].query
    assert "нет подтверждённых датированных значений" in specs[1].description
    assert len(DEFAULT_SERIES) == 13
    with pytest.raises(ValueError):
        lab_card_specs(PROFILE, (LabSeries("unknown", "Unknown", "mmol/L"),))
    with pytest.raises(TypeError):
        lab_card_specs("unsafe", ())  # type: ignore[arg-type]


def add_row(session: Session, **changes: object) -> UUID:
    document = Document(
        profile_id=changes.pop("profile_id", PROFILE),
        sha256=uuid4().hex * 2,
        vault_path="synthetic.pdf",
        media_type="application/pdf",
        document_type="lab",
        collected_date=changes.pop("date", date(2020, 1, 1)),
        processing_status=changes.pop("processing_status", "processed"),
        safe_error_code=changes.pop("safe_error_code", None),
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(
            document_id=document.id,
            page_number=1,
            extracted_text="synthetic",
            extraction_method="test",
        )
    )
    session.flush()
    values = {
        "document_id": document.id,
        "page_number": 1,
        "canonical_name": "ferritin",
        "source_name": "Ферритин",
        "source_value": "40",
        "parsed_value": Decimal(40),
        "source_unit": "ng/mL",
        "normalized_value": Decimal(40),
        "normalized_unit": "ng/mL",
        "reference_low": Decimal(10),
        "reference_high": Decimal(100),
        "reference_text": "10–100",
        "source_flag": "H",
        "evidence_excerpt": "synthetic",
        "confidence": 1,
        "status": ReviewStatus.VERIFIED,
    }
    values.update(changes)
    observation = LabObservation(**values)
    session.add(observation)
    session.flush()
    return observation.id


def test_real_queries_filter_and_preserve_repeats(db_session: Session) -> None:
    other = Profile(id=uuid4(), name="Synthetic other")
    db_session.add(other)
    db_session.flush()
    first = add_row(db_session)
    second = add_row(
        db_session,
        source_value="41",
        parsed_value=Decimal(41),
        normalized_value=Decimal(41),
    )
    add_row(db_session, source_unit="ug/L", normalized_unit="ug/L")
    add_row(db_session, source_unit="нг/мл")
    for changes in (
        {"profile_id": other.id},
        {"status": ReviewStatus.NEEDS_REVIEW},
        {"processing_status": "pending"},
        {"safe_error_code": "unsafe"},
        {"date": datetime.now(UTC).date() + timedelta(days=1)},
        {"date": None},
        {"normalized_value": Decimal("NaN")},
        {"normalized_value": Decimal("Infinity")},
        {"normalized_value": Decimal("-Infinity")},
        {"parsed_value": Decimal("Infinity")},
        {"normalized_value": Decimal("1e13")},
        {
            "source_value": "1e-13",
            "parsed_value": Decimal("1e-13"),
            "normalized_value": Decimal("1e-13"),
        },
        {"source_value": "<40"},
        {"source_value": "NaN"},
        {"source_value": "1e999999"},
        {"source_value": "42"},
        {"source_unit": "unknown"},
        {"canonical_name": "unknown"},
    ):
        add_row(db_session, **changes)
    specs = lab_card_specs(
        PROFILE, (FERRITIN, LabSeries("ferritin", "Ферритин", "ug/L"))
    )
    chart = db_session.execute(text(specs[1].query)).mappings().all()
    assert len(chart) == 3
    assert {row["observation_id"] for row in chart} >= {first, second}
    assert {row["date"] for row in chart} == {date(2020, 1, 1)}
    assert len({row["date_label"] for row in chart}) == 3
    assert {row["reference_high"] for row in chart} == {Decimal(100)}
    assert len(db_session.execute(text(specs[2].query)).all()) == 1
    detail = db_session.execute(text(specs[0].query)).mappings().all()
    assert len(detail) == 4
    assert {row["source_flag"] for row in detail} == {"H"}
    assert {row["comparison"] for row in detail} == {"within"}


@pytest.mark.parametrize(
    "reference,low,high,expected",
    [
        (None, None, None, "unknown"),
        ("<100", None, "100", "unknown"),
        ("10–100 mg/dL", "10", "100", "unknown"),
        ("10–100", "20", "100", "unknown"),
        ("50–100", "50", "100", "below"),
        ("1–10", "1", "10", "above"),
    ],
)
def test_printed_range_comparison_is_conservative(
    db_session: Session,
    reference: str | None,
    low: str | None,
    high: str | None,
    expected: str,
) -> None:
    add_row(
        db_session,
        reference_text=reference,
        reference_low=Decimal(low) if low else None,
        reference_high=Decimal(high) if high else None,
    )
    specs = lab_card_specs(PROFILE, (FERRITIN,))
    row = db_session.execute(text(specs[0].query)).mappings().one()
    assert row["comparison"] == expected
    chart = db_session.execute(text(specs[1].query)).mappings().one()
    assert (chart["reference_low"] is None) == (expected == "unknown")


class NativeMetabase(FakeMetabase):
    """Return persisted cards in the staged native shape used by Metabase."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/api/card/"):
            self.requests.append(request)
            card = deepcopy(
                next(
                    c
                    for c in self.cards
                    if c["id"] == int(request.url.path.rsplit("/", 1)[1])
                )
            )
            query = card["dataset_query"]
            card["dataset_query"] = {
                "database": query["database"],
                "stages": [{"native": query["native"]["query"]}],
            }
            return self._response(request, card)
        return super().handle(request)


def test_bootstrap_reconciles_only_owned_cards_and_preserves_user_cards(
    disposable_postgres: DisposablePostgres,
    clean_database: Engine,
) -> None:
    fake = NativeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings, engine = disposable_postgres.settings, disposable_postgres.engine
    with Session(engine) as setup_session:
        add_row(setup_session)
        setup_session.commit()
    first = bootstrap_lab_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )
    user_card = {
        "id": 999,
        "card_id": 999,
        "row": 0,
        "col": 0,
        "size_x": 24,
        "size_y": 4,
        "parameter_mappings": [],
        "visualization_settings": {"custom": True},
    }
    fake.dashboards[0]["dashcards"].append(deepcopy(user_card))
    fake.cards[1]["visualization_settings"] = {}
    second = bootstrap_lab_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )
    assert first == second and len(fake.cards) == 2
    assert str(PROFILE) in fake.dashboards[0]["name"]
    layouts = fake.dashboards[0]["dashcards"]
    assert next(c for c in layouts if c["card_id"] == first.card_ids[0])["size_x"] == 24
    assert all(c["size_x"] == 12 for c in layouts if c["card_id"] in first.card_ids[1:])
    retained = next(c for c in layouts if c["card_id"] == 999)
    assert retained["visualization_settings"] == user_card["visualization_settings"]
    assert retained["row"] == 16
    assert fake.cards[1]["visualization_settings"]["graph.show_values"] is False
    assert fake.cards[1]["visualization_settings"]["graph.show_dots"] is True
    fake.cards[1]["dataset_query"]["native"]["query"] = "SELECT 123"
    snapshot = deepcopy(fake.cards)
    with pytest.raises(ValueError, match="collision"):
        bootstrap_lab_dashboard(settings, PROFILE, transport=transport, engine=engine)
    assert fake.cards == snapshot


def test_discovery_is_registered_sorted_and_profile_scoped(
    db_session: Session,
    disposable_postgres: DisposablePostgres,
) -> None:
    add_row(
        db_session,
        canonical_name="glucose",
        source_unit="mg/dL",
        normalized_unit="mg/dL",
    )
    add_row(db_session, canonical_name="unmapped", source_unit="unknown")
    other = Profile(id=uuid4(), name="Synthetic other")
    db_session.add(other)
    db_session.flush()
    add_row(
        db_session,
        profile_id=other.id,
        canonical_name="alt",
        source_unit="U/L",
        normalized_unit="U/L",
    )
    db_session.commit()
    series = discover_lab_series(disposable_postgres.engine, PROFILE)
    assert series == (LabSeries("glucose", "Глюкоза", "mg/dL"),)
    assert len(series) <= 80


def test_detail_limit_does_not_limit_chart_history(db_session: Session) -> None:
    observation_id = add_row(db_session)
    original = db_session.get(LabObservation, observation_id)
    assert original is not None
    values = {
        column.name: getattr(original, column.name)
        for column in LabObservation.__table__.columns
        if column.name != "id"
    }
    db_session.execute(
        insert(LabObservation), [{**values, "id": uuid4()} for _ in range(1000)]
    )
    specs = lab_card_specs(PROFILE, (FERRITIN,))
    assert len(db_session.execute(text(specs[0].query)).all()) == 1000
    assert len(db_session.execute(text(specs[1].query)).all()) == 1001


def test_discovery_bound_with_all_registered_pairs(
    db_session: Session, disposable_postgres: DisposablePostgres
) -> None:
    for name, _, units in reversed(_ANALYTES):
        for unit in units.split("|"):
            add_row(
                db_session, canonical_name=name, source_unit=unit, normalized_unit=unit
            )
    db_session.commit()
    series = discover_lab_series(disposable_postgres.engine, PROFILE)
    assert len(series) == 80
    priorities = {
        (item.canonical_name, item.unit): index
        for index, item in enumerate(DEFAULT_SERIES)
    }
    positions = [
        priorities[(item.canonical_name, item.unit)]
        for item in series
        if (item.canonical_name, item.unit) in priorities
    ]
    assert positions == sorted(positions)


def test_other_profile_and_legacy_dashboard_are_untouched(
    disposable_postgres: DisposablePostgres, clean_database: Engine
) -> None:
    fake = NativeMetabase()
    legacy = {
        "id": 90,
        "name": "Анализы крови",
        "description": "User's legacy",
        "dashcards": [{"id": 91, "card_id": 92}],
    }
    fake.dashboards.append(deepcopy(legacy))
    transport = httpx.MockTransport(fake.handle)
    first = bootstrap_lab_dashboard(
        disposable_postgres.settings,
        PROFILE,
        engine=clean_database,
        transport=transport,
    )
    second = bootstrap_lab_dashboard(
        disposable_postgres.settings,
        UUID(int=2),
        engine=clean_database,
        transport=transport,
    )
    assert first.dashboard_id != second.dashboard_id
    assert set(first.card_ids).isdisjoint(second.card_ids)
    assert fake.dashboards[0] == legacy
    assert str(PROFILE) in fake.cards[0]["dataset_query"]["native"]["query"]
    assert str(UUID(int=2)) in fake.cards[1]["dataset_query"]["native"]["query"]


def test_disappearing_series_is_detached_not_deleted(
    disposable_postgres: DisposablePostgres, db_session: Session
) -> None:
    observation_id = add_row(db_session, source_unit="ug/L", normalized_unit="ug/L")
    db_session.commit()
    fake = NativeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings, engine = disposable_postgres.settings, disposable_postgres.engine
    first = bootstrap_lab_dashboard(
        settings, PROFILE, engine=engine, transport=transport
    )
    assert len(first.card_ids) == 2
    row = db_session.get(LabObservation, observation_id)
    assert row is not None
    row.status = ReviewStatus.REJECTED
    db_session.commit()
    second = bootstrap_lab_dashboard(
        settings, PROFILE, engine=engine, transport=transport
    )
    assert len(second.card_ids) == 1
    assert len(fake.cards) == 2
    assert {c["card_id"] for c in fake.dashboards[0]["dashcards"]} == set(
        second.card_ids
    )


def test_exact_legacy_owned_queries_migrate_but_custom_sql_stays_blocked(
    disposable_postgres: DisposablePostgres, db_session: Session
) -> None:
    add_row(db_session)
    db_session.commit()
    fake = NativeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings, engine = disposable_postgres.settings, disposable_postgres.engine
    bootstrap_lab_dashboard(settings, PROFILE, engine=engine, transport=transport)
    legacy = lab_dashboard.lab_card_specs(PROFILE, (FERRITIN,), _legacy=True)
    for card, spec in zip(fake.cards, legacy, strict=True):
        card["dataset_query"]["native"]["query"] = spec.query

    bootstrap_lab_dashboard(settings, PROFILE, engine=engine, transport=transport)
    assert "date_label" in fake.cards[1]["dataset_query"]["native"]["query"]

    fake.cards[1]["dataset_query"]["native"]["query"] += "\n-- user edit"
    with pytest.raises(ValueError, match="collision"):
        bootstrap_lab_dashboard(settings, PROFILE, engine=engine, transport=transport)


@pytest.mark.parametrize(
    "collision", ["duplicate", "collection", "database", "dashboard"]
)
def test_ownership_collisions_fail_without_overwriting(
    disposable_postgres: DisposablePostgres,
    clean_database: Engine,
    collision: str,
) -> None:
    fake = NativeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = disposable_postgres.settings
    bootstrap_lab_dashboard(
        settings, PROFILE, engine=clean_database, transport=transport
    )
    if collision == "duplicate":
        fake.cards.append({**deepcopy(fake.cards[0]), "id": 999})
    elif collision == "collection":
        fake.cards[0]["collection_id"] = 999
    elif collision == "database":
        fake.cards[0]["dataset_query"]["database"] = 999
    else:
        fake.dashboards[0]["description"] = "User-owned dashboard"
    before = deepcopy((fake.cards, fake.dashboards))
    with pytest.raises(ValueError, match="collision"):
        bootstrap_lab_dashboard(
            settings, PROFILE, engine=clean_database, transport=transport
        )
    assert (fake.cards, fake.dashboards) == before
