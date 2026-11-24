"""Tests for the archive planner: exactness, constraints and edge cases."""

from __future__ import annotations

import itertools
import random
from typing import Optional, Sequence

import pytest

from app.planner import (
    PlanError,
    build_segments,
    classify,
    in_band_intervals,
    plan_archive,
    reconstruct_between,
    reconstruct_run,
    validate_plan,
)


def brute_force_minimum(
    readings: Sequence[Optional[int]],
    low: int,
    high: int,
    tolerance: int,
) -> Optional[int]:
    """Smallest feasible retained count by exhaustive search (small n only)."""
    real = [offset for offset, value in enumerate(readings) if value is not None]
    if not real:
        return None
    segments = build_segments(readings)
    mandatory = {
        offset
        for segment in segments
        for offset in (segment.offset, segment.end_offset - 1)
    }
    optional = [offset for offset in real if offset not in mandatory]
    for extra_count in range(len(optional) + 1):
        for extra in itertools.combinations(optional, extra_count):
            offsets = sorted(mandatory | set(extra))
            try:
                validate_plan(readings, offsets, low, high, tolerance, len(readings))
            except PlanError:
                continue
            return len(offsets)
    return None


@pytest.mark.parametrize("seed", range(30))
def test_planner_matches_brute_force(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(1, 9)
    readings: list[Optional[int]] = []
    for _ in range(n):
        if rng.random() < 0.2:
            readings.append(None)
        else:
            readings.append(rng.randint(600, 900))
    low, high = sorted(rng.sample(range(600, 900), 2))
    tolerance = rng.randint(0, 20)

    expected = brute_force_minimum(readings, low, high, tolerance)
    if expected is None:
        with pytest.raises(PlanError):
            plan_archive(readings, low, high, tolerance, n + 1)
        return
    plan = plan_archive(readings, low, high, tolerance, n + 1)
    assert len(plan.retained_offsets) == expected
    validate_plan(
        readings, plan.retained_offsets, low, high, tolerance, len(readings)
    )


@pytest.mark.parametrize("seed", range(20))
def test_planner_respects_tolerance_and_band(seed: int) -> None:
    rng = random.Random(1000 + seed)
    n = rng.randint(5, 60)
    readings: list[Optional[int]] = []
    value = 800
    for _ in range(n):
        if rng.random() < 0.1:
            readings.append(None)
            continue
        value += rng.randint(-15, 15)
        readings.append(value)
    low = 780
    high = 840
    tolerance = rng.randint(0, 12)
    plan = plan_archive(readings, low, high, tolerance, n)
    for offset, actual in enumerate(readings):
        rebuilt = plan.reconstructed[offset]
        if actual is None:
            assert rebuilt is None
            continue
        assert abs(rebuilt - actual) <= tolerance
        assert classify(rebuilt, low, high) == classify(actual, low, high)


def test_first_and_last_of_every_segment_are_retained() -> None:
    readings = [700, 701, 702, None, None, 750, 760, None, 800]
    plan = plan_archive(readings, 600, 900, 5, 50)
    for segment in build_segments(readings):
        assert segment.offset in plan.retained_offsets
        assert segment.end_offset - 1 in plan.retained_offsets


def test_missing_minutes_are_never_bridged() -> None:
    readings = [700, 800, None, 800, 700]
    with pytest.raises(PlanError):
        # A one-point segment cannot be bridged, so this plan is invalid.
        validate_plan(readings, [0, 4], 600, 900, 100, 10)
    plan = plan_archive(readings, 600, 900, 100, 10)
    assert plan.reconstructed[2] is None
    assert sorted(plan.retained_offsets) == [0, 1, 3, 4]


def test_limit_too_small_reports_reason_and_minimum() -> None:
    readings = [700, 800, 700, 800]
    with pytest.raises(PlanError) as excinfo:
        plan_archive(readings, 600, 900, 0, 1)
    assert "needs at least" in excinfo.value.message
    assert excinfo.value.details["minimum_retained_points"] == 4
    assert excinfo.value.details["max_retained_points"] == 1


def test_constant_run_needs_two_points() -> None:
    readings = [800] * 100
    plan = plan_archive(readings, 700, 900, 0, 2)
    assert plan.retained_offsets == (0, 99)
    assert set(plan.reconstructed) == {800}


def test_straight_ramp_needs_two_points() -> None:
    readings = [700 + index for index in range(50)]
    plan = plan_archive(readings, 600, 900, 0, 2)
    assert plan.retained_offsets == (0, 49)
    assert list(plan.reconstructed) == readings


def test_single_sample_run() -> None:
    plan = plan_archive([None, 780, None], 700, 800, 0, 1)
    assert plan.retained_offsets == (1,)
    assert plan.reconstructed == (None, 780, None)


def test_all_missing_is_rejected() -> None:
    with pytest.raises(PlanError):
        plan_archive([None, None], 700, 800, 0, 5)


def test_band_limits_are_inclusive() -> None:
    assert classify(700, 700, 800) == 0
    assert classify(800, 700, 800) == 0
    assert classify(699, 700, 800) == -1
    assert classify(801, 700, 800) == 1


def test_band_change_forces_extra_point() -> None:
    # A line from 699 to 801 crosses the band, but the original stays below
    # until the very end.  The reconstruction must preserve classification.
    readings = [699, 699, 699, 699, 801]
    with pytest.raises(PlanError):
        plan_archive(readings, 700, 800, 10, 2)
    plan = plan_archive(readings, 700, 800, 10, 5)
    assert 4 in plan.retained_offsets


def test_interpolation_rounds_half_up() -> None:
    assert reconstruct_between(0, 100, 2, 101, 1) == 101  # 100.5 -> 101
    assert reconstruct_between(0, 101, 2, 100, 1) == 101  # 100.5 -> 101
    assert reconstruct_between(0, 0, 4, 10, 1) == 3  # 2.5 -> 3


def test_negative_interpolation_rounds_away_from_zero() -> None:
    assert reconstruct_between(0, 0, 4, -10, 1) == -3


def test_in_band_intervals_break_on_missing_and_outside() -> None:
    values = [None, 799, 800, 810, None, 810, 700, 800, 800, None]
    intervals = in_band_intervals(values, 700, 800, start_minute=500)
    assert intervals == [
        {"start_minute": 501, "end_minute": 502, "duration_minutes": 2},
        {"start_minute": 506, "end_minute": 508, "duration_minutes": 3},
    ]


def test_in_band_interval_runs_to_end() -> None:
    values = [800, 800, 800]
    intervals = in_band_intervals(values, 700, 800, start_minute=0)
    assert intervals == [
        {"start_minute": 0, "end_minute": 2, "duration_minutes": 3}
    ]


def test_anchor_on_missing_minute_is_rejected() -> None:
    readings = [700, 705, None, 710, 715]
    with pytest.raises(PlanError):
        reconstruct_run(readings, [0, 2, 4])
    with pytest.raises(PlanError):
        validate_plan(readings, [0, 2, 4], 600, 900, 0, 10)


def test_validate_reconstructs_from_stored_anchors() -> None:
    readings = [700, 705, 710, 715, 720]
    rebuilt = reconstruct_run(readings, [0, 4])
    assert rebuilt == readings
