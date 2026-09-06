from uuid import UUID

from health_agent.dashboard_destinations import DashboardDestinationStore


def test_destination_store_is_profile_bound_atomic_and_closed(tmp_path) -> None:
    owner = UUID(int=1)
    foreign = UUID(int=2)
    store = DashboardDestinationStore(tmp_path, "http://127.0.0.1:53000")
    store.save(owner, "labs", 7)
    store.save(owner, "whoop", 9)

    assert store.load(owner) == {"labs": 7, "whoop": 9}
    assert store.load(foreign) == {}
    assert (tmp_path / "dashboards" / f"{owner}.json").stat().st_mode & 0o777 == 0o600


def test_destination_store_rejects_remote_origin_and_invalid_record(tmp_path) -> None:
    try:
        DashboardDestinationStore(tmp_path, "https://example.com")
    except ValueError:
        pass
    else:
        raise AssertionError("remote origin accepted")

    owner = UUID(int=1)
    root = tmp_path / "dashboards"
    root.mkdir()
    (root / f"{owner}.json").write_text('{"origin":"http://evil.example","labs":7}')
    assert (
        DashboardDestinationStore(tmp_path, "http://127.0.0.1:53000").load(owner) == {}
    )


def test_destination_store_accepts_local_https_and_rejects_bool_id(tmp_path) -> None:
    store = DashboardDestinationStore(tmp_path, "https://localhost:5443")
    try:
        store.save(UUID(int=1), "labs", True)
    except ValueError:
        pass
    else:
        raise AssertionError("boolean dashboard id accepted")
    (tmp_path / "dashboards").mkdir()
    record = tmp_path / "dashboards" / f"{UUID(int=1)}.json"
    record.write_text(
        '{"profile_id":"00000000-0000-0000-0000-000000000001",'
        '"origin":"https://localhost:5443","labs":7.5}'
    )
    assert store.load(UUID(int=1)) == {}
