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


# ---------------------------------------------------------------------------
# Corrupt-archive hardening: exact integer format and structural validation
# ---------------------------------------------------------------------------


def _raw_row(store_path: str, run_id: str) -> sqlite3.Row:
    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM furnace_archives WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        connection.close()


def _execute(store_path: str, statement: str, params: tuple[Any, ...] = ()) -> None:
    connection = sqlite3.connect(store_path)
    try:
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def test_decimal_retained_point_is_rejected_not_truncated(
    client: TestClient, store_path: str
) -> None:
    # A retained value of 700.5 must never be read as the integer 700. The
    # tampered row keeps the original (integer) checksum, which the old code
    # accepted after silently truncating the float.
    assert client.post("/api/v1/archives", json=_payload()).status_code == 201
    row = _raw_row(store_path, "HT-2026-0042")
    points = json.loads(row["retained_points_json"])
    original_value = points[0][1]
    points[0][1] = original_value + 0.5
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_points_json = ? WHERE run_id = ?",
        (json.dumps(points), "HT-2026-0042"),
    )

    detail = client.get("/api/v1/archives/HT-2026-0042")
    assert detail.status_code == 500
    assert detail.json()["reason_code"] == "archive_corrupt"

    minutes = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_007},
    )
    assert minutes.status_code == 500
    assert minutes.json()["reason_code"] == "archive_corrupt"


def test_decimal_missing_range_length_is_rejected(
    client: TestClient, store_path: str
) -> None:
    readings = [800, 800, None, None, 800, 800, 800]
    assert (
        client.post(
            "/api/v1/archives",
            json=_payload(run_id="HT-GAP", readings=readings, max_retained_points=10),
        ).status_code
        == 201
    )
    row = _raw_row(store_path, "HT-GAP")
    ranges = json.loads(row["missing_ranges_json"])
    # Keep the original checksum: old code truncated 2.5 to 2 and accepted it.
    ranges[0][1] = ranges[0][1] + 0.5
    _execute(
        store_path,
        "UPDATE furnace_archives SET missing_ranges_json = ? WHERE run_id = ?",
        (json.dumps(ranges), "HT-GAP"),
    )

    response = client.get("/api/v1/archives/HT-GAP")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_boolean_retained_value_is_rejected(
    client: TestClient, store_path: str
) -> None:
    assert client.post(
        "/api/v1/archives",
        json=_payload(run_id="HT-BOOL", readings=[1, 2, 3], max_retained_points=3),
    ).status_code == 201
    row = _raw_row(store_path, "HT-BOOL")
    points = json.loads(row["retained_points_json"])
    points[0][1] = True
    # Recompute the digest over the boolean payload so only the type check can
    # catch the tampering.
    digest = db.payload_digest(
        run_id="HT-BOOL",
        start_minute_utc=row["start_minute_utc"],
        minute_count=row["minute_count"],
        band_low=row["band_low_tenths_c"],
        band_high=row["band_high_tenths_c"],
        tolerance=row["tolerance_tenths_c"],
        max_retained=row["max_retained_points"],
        retained_points=points,
        missing_ranges=json.loads(row["missing_ranges_json"]),
    )
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_points_json = ?, "
        "payload_sha256 = ? WHERE run_id = ?",
        (json.dumps(points), digest, "HT-BOOL"),
    )

    response = client.get("/api/v1/archives/HT-BOOL")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_malformed_json_payload_is_corrupt(
    client: TestClient, store_path: str
) -> None:
    assert client.post(
        "/api/v1/archives", json=_payload(run_id="HT-BROKEN-JSON")
    ).status_code == 201
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_points_json = 'not-json' "
        "WHERE run_id = ?",
        ("HT-BROKEN-JSON",),
    )
    response = client.get("/api/v1/archives/HT-BROKEN-JSON")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_malformed_point_structure_is_corrupt(
    client: TestClient, store_path: str
) -> None:
    assert client.post(
        "/api/v1/archives", json=_payload(run_id="HT-SHAPE")
    ).status_code == 201
    row = _raw_row(store_path, "HT-SHAPE")
    points = json.loads(row["retained_points_json"])
    points.append([1, 2, 3])  # entries must be two-element arrays
    digest = db.payload_digest(
        run_id="HT-SHAPE",
        start_minute_utc=row["start_minute_utc"],
        minute_count=row["minute_count"],
        band_low=row["band_low_tenths_c"],
        band_high=row["band_high_tenths_c"],
        tolerance=row["tolerance_tenths_c"],
        max_retained=row["max_retained_points"],
        retained_points=points,
        missing_ranges=json.loads(row["missing_ranges_json"]),
    )
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_points_json = ?, "
        "payload_sha256 = ? WHERE run_id = ?",
        (json.dumps(points), digest, "HT-SHAPE"),
    )
    response = client.get("/api/v1/archives/HT-SHAPE")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_overlapping_missing_ranges_are_corrupt(
    client: TestClient, store_path: str
) -> None:
    assert client.post(
        "/api/v1/archives",
        json=_payload(
            run_id="HT-OVERLAP",
            readings=[800, None, None, 800],
            max_retained_points=4,
        ),
    ).status_code == 201
    row = _raw_row(store_path, "HT-OVERLAP")
    bad_ranges = [[1, 2], [2, 1]]  # second range starts inside the first
    digest = db.payload_digest(
        run_id="HT-OVERLAP",
        start_minute_utc=row["start_minute_utc"],
        minute_count=row["minute_count"],
        band_low=row["band_low_tenths_c"],
        band_high=row["band_high_tenths_c"],
        tolerance=row["tolerance_tenths_c"],
        max_retained=row["max_retained_points"],
        retained_points=json.loads(row["retained_points_json"]),
        missing_ranges=bad_ranges,
    )
    _execute(
        store_path,
        "UPDATE furnace_archives SET missing_ranges_json = ?, "
        "payload_sha256 = ? WHERE run_id = ?",
        (json.dumps(bad_ranges), digest, "HT-OVERLAP"),
    )
    response = client.get("/api/v1/archives/HT-OVERLAP")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_malformed_checksum_string_is_corrupt(
    client: TestClient, store_path: str
) -> None:
    assert client.post(
        "/api/v1/archives", json=_payload(run_id="HT-BAD-DIGEST")
    ).status_code == 201
    _execute(
        store_path,
        "UPDATE furnace_archives SET payload_sha256 = 'not-a-sha256' "
        "WHERE run_id = ?",
        ("HT-BAD-DIGEST",),
    )
    response = client.get("/api/v1/archives/HT-BAD-DIGEST")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


def test_float_scalar_column_is_corrupt(client: TestClient, store_path: str) -> None:
    assert client.post(
        "/api/v1/archives", json=_payload(run_id="HT-FLOAT-COL")
    ).status_code == 201
    _execute(
        store_path,
        "UPDATE furnace_archives SET minute_count = 8.5 WHERE run_id = ?",
        ("HT-FLOAT-COL",),
    )
    response = client.get("/api/v1/archives/HT-FLOAT-COL")
    assert response.status_code == 500
    assert response.json()["reason_code"] == "archive_corrupt"


# ---------------------------------------------------------------------------
# List endpoint: corrupt rows are skipped and reported, never fail the list
# ---------------------------------------------------------------------------


def test_list_skips_corrupt_archives_and_reports_their_run_ids(
    client: TestClient, store_path: str
) -> None:
    for run_id in ("GOOD-1", "BAD-1", "GOOD-2"):
        assert (
            client.post("/api/v1/archives", json=_payload(run_id=run_id)).status_code
            == 201
        )
    # Distinct ingest timestamps, GOOD-2 newest and BAD-1 in the middle.
    stamps = {
        "GOOD-1": "2026-11-24T00:00:01+00:00",
        "BAD-1": "2026-11-24T00:00:02+00:00",
        "GOOD-2": "2026-11-24T00:00:03+00:00",
    }
    for run_id, stamp in stamps.items():
        _execute(
            store_path,
            "UPDATE furnace_archives SET created_at_utc = ? WHERE run_id = ?",
            (stamp, run_id),
        )
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_count = 999 WHERE run_id = 'BAD-1'",
    )

    response = client.get("/api/v1/archives", params={"limit": 50})
    assert response.status_code == 200
    body = response.json()
    assert [item["run_id"] for item in body["archives"]] == ["GOOD-2", "GOOD-1"]
    assert body["skipped_corrupt_run_ids"] == ["BAD-1"]
    assert body["available_exhausted"] is True
    assert body["limit"] == 50


def test_list_returns_all_available_when_limit_not_reached(
    client: TestClient, store_path: str
) -> None:
    for run_id in ("GOOD-1", "BAD-1", "BAD-2"):
        assert (
            client.post("/api/v1/archives", json=_payload(run_id=run_id)).status_code
            == 201
        )
    stamps = {
        "GOOD-1": "2026-11-24T00:00:01+00:00",
        "BAD-1": "2026-11-24T00:00:02+00:00",
        "BAD-2": "2026-11-24T00:00:03+00:00",
    }
    for run_id, stamp in stamps.items():
        _execute(
            store_path,
            "UPDATE furnace_archives SET created_at_utc = ? WHERE run_id = ?",
            (stamp, run_id),
        )
    for bad in ("BAD-1", "BAD-2"):
        _execute(
            store_path,
            "UPDATE furnace_archives SET retained_count = 999 WHERE run_id = ?",
            (bad,),
        )

    response = client.get("/api/v1/archives", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert [item["run_id"] for item in body["archives"]] == ["GOOD-1"]
    assert body["skipped_corrupt_run_ids"] == ["BAD-2", "BAD-1"]
    assert body["available_exhausted"] is True


def test_list_tie_breaks_same_ingest_time_by_run_id(
    client: TestClient, store_path: str
) -> None:
    for run_id in ("RUN-C", "RUN-A", "RUN-B"):
        assert (
            client.post("/api/v1/archives", json=_payload(run_id=run_id)).status_code
            == 201
        )
    # Force an identical ingest timestamp so ordering relies on run_id only.
    _execute(
        store_path,
        "UPDATE furnace_archives SET created_at_utc = '2026-11-24T00:00:00+00:00'",
    )

    response = client.get("/api/v1/archives")
    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["archives"]] == [
        "RUN-A",
        "RUN-B",
        "RUN-C",
    ]


def test_list_stops_scanning_once_limit_intact_archives_found(
    client: TestClient, store_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(8):
        assert (
            client.post(
                "/api/v1/archives", json=_payload(run_id=f"RUN-{index:02d}")
            ).status_code
            == 201
        )
    # Deterministic newest-first order, RUN-07 newest, RUN-00 oldest.
    for index in range(8):
        _execute(
            store_path,
            "UPDATE furnace_archives SET created_at_utc = ? WHERE run_id = ?",
            (f"2026-11-24T00:00:{10 + index:02d}+00:00", f"RUN-{index:02d}"),
        )
    # Two corrupt rows interspersed in list order.
    _execute(
        store_path,
        "UPDATE furnace_archives SET retained_count = 999 "
        "WHERE run_id IN ('RUN-07', 'RUN-05')",
    )

    calls: list[str] = []
    original = db.ArchiveStore._row_to_archive

    def tracking(self: db.ArchiveStore, row: sqlite3.Row) -> db.StoredArchive:
        calls.append(row["run_id"])
        return original(self, row)

    monkeypatch.setattr(db.ArchiveStore, "_row_to_archive", tracking)

    response = client.get("/api/v1/archives", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body["archives"]) == 3
    assert body["available_exhausted"] is False
    # Only the rows needed to collect three intact archives were parsed; the
    # older half of the table was never loaded.
    assert calls == ["RUN-07", "RUN-06", "RUN-05", "RUN-04", "RUN-03"]
    assert body["skipped_corrupt_run_ids"] == ["RUN-07", "RUN-05"]


def test_list_reads_one_snapshot_when_inserts_interleave(
    client: TestClient, store_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for run_id in ("RUN-1", "RUN-2", "RUN-3"):
        assert (
            client.post("/api/v1/archives", json=_payload(run_id=run_id)).status_code
            == 201
        )
    stamps = {
        "RUN-1": "2026-11-24T00:00:01+00:00",
        "RUN-2": "2026-11-24T00:00:02+00:00",
        "RUN-3": "2026-11-24T00:00:03+00:00",
    }
    for run_id, stamp in stamps.items():
        _execute(
            store_path,
            "UPDATE furnace_archives SET created_at_utc = ? WHERE run_id = ?",
            (stamp, run_id),
        )

    original = db.ArchiveStore._row_to_archive
    inserted: dict[str, bool] = {"done": False}

    def inserting_reader(
        self: db.ArchiveStore, row: sqlite3.Row
    ) -> db.StoredArchive:
        archive = original(self, row)
        # A concurrent commit lands while the list scan is in progress.
        if not inserted["done"]:
            inserted["done"] = True
            self.insert(
                run_id="RUN-LATE",
                start_minute_utc=29_000_000,
                minute_count=4,
                band_low_tenths_c=780,
                band_high_tenths_c=820,
                tolerance_tenths_c=5,
                max_retained_points=4,
                retained_points=((0, 800), (3, 800)),
                missing_ranges=(),
            )
        return archive

    monkeypatch.setattr(db.ArchiveStore, "_row_to_archive", inserting_reader)

    response = client.get("/api/v1/archives", params={"limit": 50})
    assert response.status_code == 200
    run_ids = [item["run_id"] for item in response.json()["archives"]]
    # The late commit is not part of the snapshot this list was built from.
    assert "RUN-LATE" not in run_ids
    assert run_ids == ["RUN-3", "RUN-2", "RUN-1"]

    # A subsequent list, on a fresh snapshot, does see the committed archive.
    follow_up = client.get("/api/v1/archives")
    assert "RUN-LATE" in [item["run_id"] for item in follow_up.json()["archives"]]


def test_list_with_unreadable_database_reports_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("FURNACE_DB_PATH", str(blocked))
    response = client.get("/api/v1/archives")
    assert response.status_code == 503
    assert response.json()["reason_code"] == "archive_unavailable"
