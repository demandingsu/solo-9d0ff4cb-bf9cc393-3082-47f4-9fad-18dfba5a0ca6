"""API and SQLite storage tests for the furnace cold archive."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture()
def store_path(tmp_path: Any) -> str:
    return str(tmp_path / "archive.sqlite3")


@pytest.fixture()
def client(store_path: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FURNACE_DB_PATH", store_path)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "HT-2026-0042",
        "start_minute_utc": 29_000_000,
        "readings": [700, 750, 800, 800, 800, 790, 780, 780],
        "band_low_tenths_c": 780,
        "band_high_tenths_c": 820,
        "tolerance_tenths_c": 5,
        "max_retained_points": 8,
    }
    payload.update(overrides)
    return payload


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_docs_available(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200


def test_create_and_query_roundtrip(client: TestClient) -> None:
    created = client.post("/api/v1/archives", json=_payload())
    assert created.status_code == 201, created.text
    body = created.json()
    summary = body["archive"]
    assert summary["run_id"] == "HT-2026-0042"
    assert summary["retained_points_count"] <= 8
    assert summary["measured_minutes"] == 8
    assert summary["missing_minutes"] == 0

    queried = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_007},
    )
    assert queried.status_code == 200
    data = queried.json()
    assert [m["value_tenths_c"] for m in data["minutes"]] == [
        700,
        750,
        800,
        800,
        795,
        790,
        785,
        780,
    ]
    # Every reconstructed minute is within the 5-tenths tolerance of the
    # original reading and keeps its band state.
    originals = [700, 750, 800, 800, 800, 790, 780, 780]
    for measured, reconstructed in zip(originals, data["minutes"]):
        assert abs(reconstructed["value_tenths_c"] - measured) <= 5
    assert data["missing_minutes"] == []
    # 780..800 are inside [780, 820]; 750 and 700 are below.
    assert data["in_band_intervals"] == [
        {"start_minute_utc": 29_000_002, "end_minute_utc": 29_000_007, "duration_minutes": 6}
    ]


def test_database_stores_only_compressed_archive(
    client: TestClient, store_path: str
) -> None:
    readings = [800] * 60
    response = client.post(
        "/api/v1/archives",
        json=_payload(run_id="HT-CONST", readings=readings, max_retained_points=2),
    )
    assert response.status_code == 201, response.text
    assert response.json()["archive"]["retained_points_count"] == 2

    connection = sqlite3.connect(store_path)
    try:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(furnace_archives)")
        ]
    finally:
        connection.close()
    # No raw reading column or per-minute table exists.
    assert not any("readings" in name and "json" not in name for name in columns)
    assert "readings_json" not in columns
    with open(store_path, "rb") as handle:
        blob = handle.read()
    # The raw 60-minute series is not present as a stored array; only the two
    # retained anchors and metadata are.
    assert b"800,800,800,800" not in blob


def test_missing_minutes_are_marked_and_break_intervals(client: TestClient) -> None:
    readings = [800, 800, None, None, 800, 800, 800]
    response = client.post(
        "/api/v1/archives",
        json=_payload(run_id="HT-GAP", readings=readings, max_retained_points=10),
    )
    assert response.status_code == 201, response.text
    queried = client.get(
        "/api/v1/archives/HT-GAP/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_006},
    )
    data = queried.json()
    assert data["missing_minutes"] == [29_000_002, 29_000_003]
    assert [m["missing"] for m in data["minutes"]] == [
        False,
        False,
        True,
        True,
        False,
        False,
        False,
    ]
    assert data["in_band_intervals"] == [
        {"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_001, "duration_minutes": 2},
        {"start_minute_utc": 29_000_004, "end_minute_utc": 29_000_006, "duration_minutes": 3},
    ]


def test_query_is_limited_to_requested_range(client: TestClient) -> None:
    client.post("/api/v1/archives", json=_payload())
    response = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_003, "end_minute_utc": 29_000_004},
    )
    data = response.json()
    assert [m["minute_utc"] for m in data["minutes"]] == [29_000_003, 29_000_004]
    assert [m["value_tenths_c"] for m in data["minutes"]] == [800, 795]


def test_band_state_labels_are_inclusive(client: TestClient) -> None:
    readings = [779, 780, 820, 821]
    client.post(
        "/api/v1/archives",
        json=_payload(
            run_id="HT-BAND",
            readings=readings,
            tolerance_tenths_c=0,
            max_retained_points=4,
        ),
    )
    response = client.get(
        "/api/v1/archives/HT-BAND/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_003},
    )
    assert [m["band_state"] for m in response.json()["minutes"]] == [
        "below",
        "in_band",
        "in_band",
        "above",
    ]


def test_infeasible_plan_returns_reason_and_writes_nothing(
    client: TestClient, store_path: str
) -> None:
    response = client.post(
        "/api/v1/archives",
        json=_payload(
            run_id="HT-TOO-TIGHT",
            readings=[700, 800, 700, 800],
            tolerance_tenths_c=0,
            max_retained_points=1,
        ),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["reason_code"] == "no_feasible_plan"
    assert body["minimum_retained_points"] == 4
    assert body["max_retained_points"] == 1
    assert "needs at least" in body["detail"]
    assert client.get("/api/v1/archives/HT-TOO-TIGHT").status_code == 404


def test_duplicate_run_is_rejected_and_original_untouched(client: TestClient) -> None:
    first = client.post("/api/v1/archives", json=_payload())
    assert first.status_code == 201
    second = client.post(
        "/api/v1/archives",
        json=_payload(readings=[800, 800, 800, 800, 800, 800, 800, 800]),
    )
    assert second.status_code == 409
    assert second.json()["reason_code"] == "run_already_archived"
    queried = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_000},
    )
    assert queried.json()["minutes"][0]["value_tenths_c"] == 700


def test_existing_archives_survive_write_failure(
    client: TestClient, store_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.post("/api/v1/archives", json=_payload()).status_code == 201

    def boom(self: db.ArchiveStore, **_: Any) -> db.StoredArchive:
        raise db.ArchiveWriteError("disk full (simulated)")

    original_insert = db.ArchiveStore.insert
    monkeypatch.setattr(db.ArchiveStore, "insert", boom)
    failed = client.post(
        "/api/v1/archives", json=_payload(run_id="HT-WILL-FAIL")
    )
    assert failed.status_code == 503
    body = failed.json()
    assert body["reason_code"] == "archive_write_failed"
    assert body["retryable"] is True
    assert "disk full" in body["detail"]
    monkeypatch.setattr(db.ArchiveStore, "insert", original_insert)

    # The earlier archive is still readable and new writes work again.
    assert client.get("/api/v1/archives/HT-2026-0042").status_code == 200
    assert client.get("/api/v1/archives/HT-WILL-FAIL").status_code == 404
    assert (
        client.post(
            "/api/v1/archives", json=_payload(run_id="HT-WILL-FAIL")
        ).status_code
        == 201
    )


def test_query_unknown_run_returns_404(client: TestClient) -> None:
    response = client.get(
        "/api/v1/archives/NOPE/minutes",
        params={"start_minute_utc": 0, "end_minute_utc": 10},
    )
    assert response.status_code == 404
    assert response.json()["reason_code"] == "archive_not_found"


def test_query_range_outside_run_returns_416(client: TestClient) -> None:
    client.post("/api/v1/archives", json=_payload())
    response = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 28_999_999, "end_minute_utc": 29_000_007},
    )
    assert response.status_code == 416
    assert response.json()["reason_code"] == "range_outside_run"


def test_query_reversed_range_returns_422(client: TestClient) -> None:
    client.post("/api/v1/archives", json=_payload())
    response = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_005, "end_minute_utc": 29_000_001},
    )
    assert response.status_code == 422


def test_payload_validation_errors(client: TestClient) -> None:
    inverted = client.post(
        "/api/v1/archives",
        json=_payload(band_low_tenths_c=900, band_high_tenths_c=800),
    )
    assert inverted.status_code == 422

    all_missing = client.post(
        "/api/v1/archives",
        json=_payload(run_id="HT-EMPTY", readings=[None, None]),
    )
    assert all_missing.status_code == 422

    too_many = client.post(
        "/api/v1/archives",
        json=_payload(run_id="HT-LONG", readings=[800] * 361),
    )
    assert too_many.status_code == 422


def test_list_archives(client: TestClient) -> None:
    for run_id in ("A", "B", "C"):
        assert (
            client.post("/api/v1/archives", json=_payload(run_id=run_id)).status_code
            == 201
        )
    response = client.get("/api/v1/archives", params={"limit": 2})
    assert response.status_code == 200
    assert len(response.json()["archives"]) == 2


def test_stored_archive_integrity_is_checked(client: TestClient, store_path: str) -> None:
    assert client.post("/api/v1/archives", json=_payload()).status_code == 201
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT retained_points_json FROM furnace_archives WHERE run_id = ?",
            ("HT-2026-0042",),
        ).fetchone()
        points = json.loads(row[0])
        points[0][1] += 1  # tamper with a retained value
        connection.execute(
            "UPDATE furnace_archives SET retained_points_json = ? WHERE run_id = ?",
            (json.dumps(points), "HT-2026-0042"),
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_007},
    )
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_tampered_archive_does_not_hide_healthy_ones(
    client: TestClient, store_path: str
) -> None:
    assert client.post("/api/v1/archives", json=_payload(run_id="GOOD")).status_code == 201
    assert client.post("/api/v1/archives", json=_payload(run_id="BAD")).status_code == 201
    connection = sqlite3.connect(store_path)
    try:
        connection.execute(
            "UPDATE furnace_archives SET retained_count = 999 WHERE run_id = 'BAD'"
        )
        connection.commit()
    finally:
        connection.close()

    assert client.get("/api/v1/archives/GOOD").status_code == 200
    assert client.get("/api/v1/archives/BAD").status_code == 500


def test_unreachable_database_reports_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Point at a directory path: SQLite cannot open it as a database file.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("FURNACE_DB_PATH", str(blocked))

    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["database"] == "unavailable"

    created = client.post("/api/v1/archives", json=_payload(run_id="HT-NODB"))
    assert created.status_code == 503
    assert created.json()["reason_code"] == "archive_unavailable"

    queried = client.get(
        "/api/v1/archives/HT-NODB/minutes",
        params={"start_minute_utc": 0, "end_minute_utc": 1},
    )
    assert queried.status_code == 503
    assert queried.json()["reason_code"] == "archive_unavailable"
