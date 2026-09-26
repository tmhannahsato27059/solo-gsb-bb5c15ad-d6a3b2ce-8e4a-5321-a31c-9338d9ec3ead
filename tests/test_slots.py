"""Unit tests for the slot-insertion domain logic."""

from __future__ import annotations

from datetime import datetime, timezone

from app.separation import (
    Capacity,
    ClosureWindow,
    Flight,
    build_matrix,
    find_insert_slot,
)

UTC = timezone.utc


def dt(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def make(no: str, t: int, wake: str) -> Flight:
    return Flight(flight_no=no, scheduled_off=dt(t), wake=wake)


def window(start: int, end: int) -> ClosureWindow:
    return ClosureWindow(start=dt(start), end=dt(end))


DEFAULT = build_matrix(None)
# matrix where every pair requires 0 s (only M->M listed, rest unspecified)
ZERO = build_matrix({("M", "M"): 0})


def insert(flights, matrix=DEFAULT, windows=None, capacity=None, **candidate):
    return find_insert_slot(
        flights, matrix, windows, capacity, **candidate
    )


def test_insert_between_flights_respects_wake_both_sides():
    # H@0 -> M needs 120; candidate M -> M@300 needs 60 (t <= 240)
    flights = [make("CA1", 0, "H"), make("CA2", 300, "M")]
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=60, latest=240
    )
    assert result.feasible
    assert result.off_time == dt(120)
    assert result.position == 2
    assert result.order == ["CA1", "CA9", "CA2"]
    assert result.predecessor.flight_no == "CA1"
    assert result.predecessor.required_seconds == 120
    assert result.predecessor.actual_seconds == 120
    assert result.successor.flight_no == "CA2"
    assert result.successor.required_seconds == 60
    assert result.successor.actual_seconds == 180


def test_insert_interval_includes_both_ends():
    flights = [make("CA1", 0, "H"), make("CA2", 300, "M")]
    # earliest bound itself is the first feasible instant
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=120, latest=240
    )
    assert result.feasible and result.off_time == dt(120)
    # latest bound itself is feasible (single-instant window)
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=240, latest=240
    )
    assert result.feasible and result.off_time == dt(240)
    # one second earlier than the wake bound -> no slot
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=60, latest=119
    )
    assert not result.feasible


def test_insert_before_all_and_after_all():
    flights = [make("CA1", 300, "M"), make("CA2", 600, "M")]
    # before everything: only the successor wake gap binds (t <= 300-60)
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=0, latest=1000
    )
    assert result.feasible and result.off_time == dt(0) and result.position == 1
    assert result.predecessor is None
    assert result.successor.flight_no == "CA1"
    # after everything: only the predecessor wake gap binds (t >= 600+60)
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=650, latest=1000
    )
    assert result.feasible and result.off_time == dt(660) and result.position == 3
    assert result.predecessor.flight_no == "CA2"
    assert result.successor is None


def test_insert_same_time_tie_broken_by_flight_no():
    flights = [make("CA1", 60, "M"), make("CA2", 300, "M")]
    # smaller flight number slots in before the flight at the same instant
    result = insert(
        flights, ZERO, candidate_no="CA0", candidate_wake="M", earliest=60, latest=60
    )
    assert result.feasible and result.off_time == dt(60)
    assert result.order == ["CA0", "CA1", "CA2"]
    # larger flight number slots in after it
    result = insert(
        flights, ZERO, candidate_no="CA9", candidate_wake="M", earliest=60, latest=60
    )
    assert result.feasible and result.off_time == dt(60)
    assert result.order == ["CA1", "CA9", "CA2"]


def test_insert_closure_window_push_and_chain():
    flights = [make("CA1", 0, "M"), make("CA2", 600, "M")]
    wins = [window(60, 120), window(120, 180)]  # touching windows chain
    result = insert(
        flights,
        ZERO,
        windows=wins,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=60,
        latest=300,
    )
    assert result.feasible and result.off_time == dt(180)
    # window end exactly at the upper bound is still feasible
    result = insert(
        flights,
        ZERO,
        windows=wins,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=60,
        latest=180,
    )
    assert result.feasible and result.off_time == dt(180)
    # one second less -> the closure leaves no room in the window
    result = insert(
        flights,
        ZERO,
        windows=wins,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=60,
        latest=179,
    )
    assert not result.feasible


def test_insert_capacity_follower_and_anchor_bounds():
    # 0, 300, 600 with "max 2 per 600 s": the only feasible slot is t >= 900
    flights = [make("CA1", 0, "M"), make("CA2", 300, "M"), make("CA3", 600, "M")]
    cap = Capacity(window_seconds=600, max_departures=2)
    result = insert(
        flights,
        ZERO,
        capacity=cap,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=0,
        latest=1200,
    )
    assert result.feasible and result.off_time == dt(900) and result.position == 4
    # window closing before 900 -> no slot
    result = insert(
        flights,
        ZERO,
        capacity=cap,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=0,
        latest=899,
    )
    assert not result.feasible


def test_insert_capacity_shifted_anchor_blocks_index():
    # Inserting between C@1200 and D@1500 would make D's anchor move from
    # B@300 to C@1200 (300 s < 600 s window) — infeasible at any time there,
    # even though the candidate's own follower/anchor bounds would pass.
    flights = [
        make("A", 0, "M"),
        make("B", 300, "M"),
        make("C", 1200, "M"),
        make("D", 1500, "M"),
    ]
    cap = Capacity(window_seconds=600, max_departures=2)
    result = insert(
        flights,
        ZERO,
        capacity=cap,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=1201,
        latest=1499,
    )
    assert not result.feasible
    assert result.order == ["A", "B", "C", "D"]
    # ... but appending after D works once the follower bound (1200+600) is met
    result = insert(
        flights,
        ZERO,
        capacity=cap,
        candidate_no="CA9",
        candidate_wake="M",
        earliest=1201,
        latest=2000,
    )
    assert result.feasible and result.off_time == dt(1800) and result.position == 5


def test_insert_infeasible_returns_existing_order_and_no_slot():
    flights = [make("CA1", 0, "J"), make("CA2", 180, "M")]
    # J->M needs 180 and M->M needs 60: no room between 0 and 180 at all
    result = insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=60, latest=120
    )
    assert not result.feasible
    assert result.off_time is None
    assert result.position is None
    assert result.predecessor is None and result.successor is None
    assert result.order == ["CA1", "CA2"]


def test_insert_does_not_mutate_inputs():
    flights = [make("CA2", 300, "M"), make("CA1", 0, "H")]
    insert(
        flights, candidate_no="CA9", candidate_wake="M", earliest=60, latest=240
    )
    # input list order and scheduled times untouched
    assert [f.flight_no for f in flights] == ["CA2", "CA1"]
    assert flights[0].scheduled_off == dt(300)
    assert flights[1].scheduled_off == dt(0)
