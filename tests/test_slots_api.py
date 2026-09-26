"""End-to-end tests for POST /api/v1/slots/insert."""

from __future__ import annotations

from app.database import get_conn


def _flight(no: str, off: str, wake: str) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake}


def _body(**overrides) -> dict:
    body = {
        "runway": {
            "runway": "36L",
            "flights": [
                _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                _flight("CA2", "2026-09-18T00:05:00Z", "M"),
            ],
        },
        "candidate": {
            "flight_no": "CA9",
            "wake": "M",
            "earliest_off": "2026-09-18T00:01:00Z",
            "latest_off": "2026-09-18T00:04:00Z",
        },
    }
    body.update(overrides)
    return body


def test_insert_feasible_basic(client):
    r = client.post("/api/v1/slots/insert", json=_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["runway"] == "36L"
    assert data["feasible"] is True
    # H->M requires 120 s: earliest 00:01:00 pushed to 00:02:00
    assert data["slot_off"] == "2026-09-18T00:02:00Z"
    assert data["position"] == 2
    assert data["order"] == ["CA1", "CA9", "CA2"]
    assert data["reason"] is None
    pred = data["predecessor"]
    assert pred["flight_no"] == "CA1"
    assert pred["required_seconds"] == 120
    assert pred["actual_seconds"] == 120
    succ = data["successor"]
    assert succ["flight_no"] == "CA2"
    assert succ["required_seconds"] == 60
    assert succ["actual_seconds"] == 180
    # candidate window echoed back normalised to UTC
    assert data["candidate_flight_no"] == "CA9"
    assert data["earliest_off"] == "2026-09-18T00:01:00Z"
    assert data["latest_off"] == "2026-09-18T00:04:00Z"
    assert data["matrix"]["H"]["M"] == 120
    # read-only query: no batch handle
    assert "batch_id" not in data


def test_insert_timezone_normalisation(client):
    body = _body()
    body["candidate"]["earliest_off"] = "2026-09-18T08:01:00+08:00"
    body["candidate"]["latest_off"] = "2026-09-18T08:04:00+08:00"
    data = client.post("/api/v1/slots/insert", json=body).json()
    assert data["slot_off"] == "2026-09-18T00:02:00Z"
    assert data["earliest_off"] == "2026-09-18T00:01:00Z"
    assert data["latest_off"] == "2026-09-18T00:04:00Z"


def test_insert_closure_window_push(client):
    body = _body()
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:02:00Z", "end": "2026-09-18T00:02:30Z"}
    ]
    data = client.post("/api/v1/slots/insert", json=body).json()
    # 00:02:00 falls inside the closure -> pushed to its end 00:02:30
    assert data["feasible"] is True
    assert data["slot_off"] == "2026-09-18T00:02:30Z"


def test_insert_capacity_limits_merged_sequence(client):
    body = _body(
        runway={
            "runway": "36L",
            "flights": [
                _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                _flight("CA2", "2026-09-18T00:05:00Z", "M"),
                _flight("CA3", "2026-09-18T00:10:00Z", "M"),
            ],
            "capacity": {"window_seconds": 600, "max_departures": 2},
        },
        matrix=[{"leader": "M", "follower": "M", "seconds": 0}],
    )
    body["candidate"]["earliest_off"] = "2026-09-18T00:00:00Z"
    body["candidate"]["latest_off"] = "2026-09-18T00:20:00Z"
    data = client.post("/api/v1/slots/insert", json=body).json()
    # max 2 departures per 600 s: earliest feasible is 00:05:00 + 600 s
    assert data["feasible"] is True
    assert data["slot_off"] == "2026-09-18T00:15:00Z"
    assert data["position"] == 4
    assert data["order"] == ["CA1", "CA2", "CA3", "CA9"]


def test_insert_no_slot_returns_clear_result(client):
    body = _body()
    body["runway"]["flights"] = [
        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
        _flight("CA2", "2026-09-18T00:03:00Z", "M"),
    ]
    body["candidate"]["latest_off"] = "2026-09-18T00:01:30Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is False
    assert data["slot_off"] is None
    assert data["position"] is None
    assert data["predecessor"] is None
    assert data["successor"] is None
    assert data["order"] == ["CA1", "CA2"]
    assert data["reason"]


def test_insert_existing_wake_violation_is_422(client):
    body = _body()
    body["runway"]["flights"] = [
        _flight("CA1", "2026-09-18T00:00:00Z", "J"),
        _flight("CA2", "2026-09-18T00:01:00Z", "M"),  # 60 s < J->M 180 s
    ]
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert len(detail["wake_violations"]) == 1
    assert detail["wake_violations"][0]["leader"] == "CA1"
    assert detail["wake_violations"][0]["follower"] == "CA2"
    assert detail["closure_conflicts"] == []
    assert detail["capacity_violations"] == []


def test_insert_existing_closure_conflict_is_422(client):
    body = _body()
    body["runway"]["closures"] = [
        {"start": "2026-09-17T23:59:00Z", "end": "2026-09-18T00:01:00Z"}
    ]  # CA1@00:00:00 falls inside
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert [c["flight_no"] for c in detail["closure_conflicts"]] == ["CA1"]
    assert detail["wake_violations"] == []


def test_insert_existing_capacity_violation_is_422(client):
    body = _body(
        runway={
            "runway": "36L",
            "flights": [
                _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                _flight("CA2", "2026-09-18T00:05:00Z", "M"),
                _flight("CA3", "2026-09-18T00:06:00Z", "M"),
            ],
            "capacity": {"window_seconds": 600, "max_departures": 2},
        },
        matrix=[{"leader": "M", "follower": "M", "seconds": 0}],
    )
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert [cv["flight_no"] for cv in detail["capacity_violations"]] == ["CA3"]
    assert detail["capacity_violations"][0]["anchor_flight_no"] == "CA1"


def test_insert_candidate_flight_no_must_be_unique(client):
    body = _body()
    body["candidate"]["flight_no"] = "CA1"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "重复" in r.text


def test_insert_window_must_not_be_inverted(client):
    body = _body()
    body["candidate"]["earliest_off"] = "2026-09-18T00:04:00Z"
    body["candidate"]["latest_off"] = "2026-09-18T00:01:00Z"
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422
    # equal bounds are allowed (single instant)
    body["candidate"]["earliest_off"] = "2026-09-18T00:02:00Z"
    body["candidate"]["latest_off"] = "2026-09-18T00:02:00Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 200
    assert r.json()["slot_off"] == "2026-09-18T00:02:00Z"


def test_insert_candidate_times_require_timezone_and_whole_seconds(client):
    body = _body()
    body["candidate"]["earliest_off"] = "2026-09-18T00:01:00"  # naive
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422
    body = _body()
    body["candidate"]["latest_off"] = "2026-09-18T00:04:00.500Z"  # sub-second
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422


def test_insert_does_not_persist_batches(client):
    def counts() -> tuple[int, int]:
        with get_conn() as conn:
            batches = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
            flights = conn.execute("SELECT COUNT(*) AS c FROM flights").fetchone()["c"]
        return batches, flights

    before = counts()
    r = client.post("/api/v1/slots/insert", json=_body())
    assert r.status_code == 200
    assert counts() == before  # pure query: no batch, no plan rows written


def test_insert_matrix_duplicate_pair_is_422(client):
    body = _body(
        matrix=[
            {"leader": "H", "follower": "M", "seconds": 120},
            {"leader": "H", "follower": "M", "seconds": 90},
        ]
    )
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422
