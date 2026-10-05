from datetime import UTC, datetime, timedelta

import pytest

from health_agent.research.quality import coverage, nightly_analysis_coverage_ok


@pytest.mark.parametrize("gap,accepted", [(66, True), (600, True), (606, False)])
def test_nightly_gap_rule_is_inclusive_and_not_collection_status(gap, accepted):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=8)
    points = [
        start + timedelta(seconds=offset)
        for offset in range(0, 8 * 3600, 6)
        if not 60 < offset < 60 + gap
    ]
    check = coverage(points, start, end)
    assert check["largest_gap_seconds"] == gap
    assert check["status"] == "gaps"  # Operational warning is still visible.
    assert nightly_analysis_coverage_ok(check) is accepted
    if gap == 600:
        assert check["coverage_percent"] < 99


@pytest.mark.parametrize("edge", ["start", "end"])
@pytest.mark.parametrize("gap,accepted", [(600, True), (606, False)])
def test_nightly_analysis_counts_missing_sleep_boundaries(edge, gap, accepted):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = start + timedelta(hours=8)
    points = [
        start + timedelta(seconds=offset)
        for offset in range(0, 8 * 3600, 6)
        if (offset >= gap if edge == "start" else offset <= 8 * 3600 - gap)
    ]
    assert nightly_analysis_coverage_ok(coverage(points, start, end)) is accepted


def test_empty_series_is_not_usable_even_for_short_interval():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    assert not nightly_analysis_coverage_ok(
        coverage([], start, start + timedelta(minutes=5))
    )
