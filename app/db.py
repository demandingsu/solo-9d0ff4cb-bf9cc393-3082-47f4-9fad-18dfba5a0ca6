"""SQLite persistence for compressed furnace archives.

Only the compressed archive is stored: retained sample points plus the ranges
of missing minutes.  Raw per-minute readings are never written to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from .planner import (
    MAX_RUN_MINUTES,
    PlanError,
    Span,
    reconstruct_spans,
    spans_from_missing_ranges,
)

SCHEMA_VERSION = "1"
PLANNER_VERSION = "1.0.0"
DEFAULT_DB_PATH = os.environ.get("FURNACE_DB_PATH", "/data/furnace_archive.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS furnace_archives (
    run_id                 TEXT PRIMARY KEY,
    start_minute_utc       INTEGER NOT NULL,
    minute_count           INTEGER NOT NULL
                           CHECK (minute_count > 0
                                  AND minute_count <= 360),
    band_low_tenths_c      INTEGER NOT NULL,
    band_high_tenths_c     INTEGER NOT NULL,
    tolerance_tenths_c     INTEGER NOT NULL CHECK (tolerance_tenths_c >= 0),
    max_retained_points    INTEGER NOT NULL CHECK (max_retained_points >= 0),
    retained_count         INTEGER NOT NULL CHECK (retained_count >= 0),
    retained_points_json   TEXT NOT NULL,
    missing_ranges_json    TEXT NOT NULL,
    payload_sha256         TEXT NOT NULL,
    planner_version        TEXT NOT NULL,
    created_at_utc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archives_start
    ON furnace_archives (start_minute_utc);
"""


class ArchiveConflict(Exception):
    """The run id is already archived."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} is already archived")
        self.run_id = run_id


class ArchiveWriteError(Exception):
    """The archive could not be written."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ArchiveUnavailable(Exception):
    """The archive database cannot be opened or reached."""


class ArchiveCorrupt(Exception):
    """A stored archive failed its integrity checks."""


@dataclass(frozen=True)
class StoredArchive:
    run_id: str
    start_minute_utc: int
    minute_count: int
    band_low_tenths_c: int
    band_high_tenths_c: int
    tolerance_tenths_c: int
    max_retained_points: int
    retained_count: int
    retained_points: tuple[tuple[int, int], ...]
    missing_ranges: tuple[tuple[int, int], ...]
    payload_sha256: str
    planner_version: str
    created_at_utc: str

    @property
    def spans(self) -> tuple[Span, ...]:
        return spans_from_missing_ranges(self.minute_count, self.missing_ranges)

    @property
    def end_minute_utc(self) -> int:
        """Inclusive last minute of the run."""
        return self.start_minute_utc + self.minute_count - 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def missing_ranges_from_readings(
    readings: Sequence[Optional[int]],
) -> tuple[tuple[int, int], ...]:
    """Compress missing minutes into maximal ``[offset, length]`` ranges."""
    ranges: list[tuple[int, int]] = []
    start: Optional[int] = None
    for offset, value in enumerate(readings):
        if value is None:
            if start is None:
                start = offset
        elif start is not None:
            ranges.append((start, offset - start))
            start = None
    if start is not None:
        ranges.append((start, len(readings) - start))
    return tuple(ranges)


def _canonical_payload(
    *,
    run_id: str,
    start_minute_utc: int,
    minute_count: int,
    band_low: int,
    band_high: int,
    tolerance: int,
    max_retained: int,
    retained_points: Sequence[Sequence[int]],
    missing_ranges: Sequence[Sequence[int]],
) -> bytes:
    payload = {
        "run_id": run_id,
        "start_minute_utc": start_minute_utc,
        "minute_count": minute_count,
        "band_low_tenths_c": band_low,
        "band_high_tenths_c": band_high,
        "tolerance_tenths_c": tolerance,
        "max_retained_points": max_retained,
        "retained_points": [list(point) for point in retained_points],
        "missing_ranges": [list(item) for item in missing_ranges],
        "planner_version": PLANNER_VERSION,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def payload_digest(
    *,
    run_id: str,
    start_minute_utc: int,
    minute_count: int,
    band_low: int,
    band_high: int,
    tolerance: int,
    max_retained: int,
    retained_points: Sequence[Sequence[int]],
    missing_ranges: Sequence[Sequence[int]],
) -> str:
    return hashlib.sha256(
        _canonical_payload(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low=band_low,
            band_high=band_high,
            tolerance=tolerance,
            max_retained=max_retained,
            retained_points=retained_points,
            missing_ranges=missing_ranges,
        )
    ).hexdigest()


class ArchiveStore:
    """A tiny connection-per-operation SQLite store.

    Writes run in ``BEGIN IMMEDIATE`` transactions and roll back completely on
    failure, so a failed write never disturbs archives already stored.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH, *, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(
                f"cannot open archive database at {self.path!r}: {exc}"
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        except sqlite3.Error as exc:
            connection.close()
            raise ArchiveUnavailable(
                f"cannot use archive database at {self.path!r}: {exc}"
            ) from exc
        return connection

    def init(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ArchiveUnavailable(
                f"cannot create archive directory {directory!r}: {exc}"
            ) from exc
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(
                f"cannot initialise archive database at {self.path!r}: {exc}"
            ) from exc
        finally:
            connection.close()

    def healthy(self) -> bool:
        try:
            connection = self._connect()
            try:
                connection.execute("SELECT 1 FROM furnace_archives LIMIT 1")
            finally:
                connection.close()
            return True
        except (ArchiveUnavailable, sqlite3.Error, OSError):
            return False

    def insert(
        self,
        *,
        run_id: str,
        start_minute_utc: int,
        minute_count: int,
        band_low_tenths_c: int,
        band_high_tenths_c: int,
        tolerance_tenths_c: int,
        max_retained_points: int,
        retained_points: Sequence[tuple[int, int]],
        missing_ranges: Sequence[tuple[int, int]],
    ) -> StoredArchive:
        if minute_count <= 0 or minute_count > MAX_RUN_MINUTES:
            raise ArchiveWriteError(
                f"minute_count must be between 1 and {MAX_RUN_MINUTES}"
            )
        points_json = json.dumps(
            [list(point) for point in retained_points], separators=(",", ":")
        )
        ranges_json = json.dumps(
            [list(item) for item in missing_ranges], separators=(",", ":")
        )
        digest = payload_digest(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low=band_low_tenths_c,
            band_high=band_high_tenths_c,
            tolerance=tolerance_tenths_c,
            max_retained=max_retained_points,
            retained_points=retained_points,
            missing_ranges=missing_ranges,
        )
        created_at = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM furnace_archives WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is not None:
                connection.execute("ROLLBACK")
                raise ArchiveConflict(run_id)
            connection.execute(
                """
                INSERT INTO furnace_archives (
                    run_id, start_minute_utc, minute_count,
                    band_low_tenths_c, band_high_tenths_c, tolerance_tenths_c,
                    max_retained_points, retained_count, retained_points_json,
                    missing_ranges_json, payload_sha256, planner_version,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    start_minute_utc,
                    minute_count,
                    band_low_tenths_c,
                    band_high_tenths_c,
                    tolerance_tenths_c,
                    max_retained_points,
                    len(retained_points),
                    points_json,
                    ranges_json,
                    digest,
                    PLANNER_VERSION,
                    created_at,
                ),
            )
            connection.execute("COMMIT")
        except ArchiveConflict:
            raise
        except sqlite3.IntegrityError as exc:
            self._safe_rollback(connection)
            if "UNIQUE" in str(exc).upper():
                raise ArchiveConflict(run_id) from exc
            raise ArchiveWriteError(
                f"database rejected the archive: {exc}", retryable=False
            ) from exc
        except (sqlite3.Error, OSError) as exc:
            self._safe_rollback(connection)
            raise ArchiveWriteError(f"failed to write archive: {exc}") from exc
        finally:
            connection.close()

        return StoredArchive(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low_tenths_c=band_low_tenths_c,
            band_high_tenths_c=band_high_tenths_c,
            tolerance_tenths_c=tolerance_tenths_c,
            max_retained_points=max_retained_points,
            retained_count=len(retained_points),
            retained_points=tuple(retained_points),
            missing_ranges=tuple(missing_ranges),
            payload_sha256=digest,
            planner_version=PLANNER_VERSION,
            created_at_utc=created_at,
        )

    @staticmethod
    def _safe_rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _row_to_archive(self, row: sqlite3.Row) -> StoredArchive:
        try:
            retained_raw = json.loads(row["retained_points_json"])
            missing_raw = json.loads(row["missing_ranges_json"])
        except (TypeError, ValueError) as exc:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} has unreadable JSON payload"
            ) from exc

        retained: list[tuple[int, int]] = []
        try:
            for item in retained_raw:
                if len(item) != 2:
                    raise ValueError("retained point must have two entries")
                offset, value = int(item[0]), int(item[1])
                retained.append((offset, value))
            missing: list[tuple[int, int]] = []
            for item in missing_raw:
                if len(item) != 2:
                    raise ValueError("missing range must have two entries")
                start, length = int(item[0]), int(item[1])
                missing.append((start, length))
        except (TypeError, ValueError, IndexError) as exc:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} has malformed point data"
            ) from exc

        expected = payload_digest(
            run_id=row["run_id"],
            start_minute_utc=row["start_minute_utc"],
            minute_count=row["minute_count"],
            band_low=row["band_low_tenths_c"],
            band_high=row["band_high_tenths_c"],
            tolerance=row["tolerance_tenths_c"],
            max_retained=row["max_retained_points"],
            retained_points=retained,
            missing_ranges=missing,
        )
        if expected != row["payload_sha256"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} failed its integrity checksum"
            )
        if len(retained) != row["retained_count"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} retained count does not match payload"
            )
        if len(retained) > row["max_retained_points"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} exceeds its own retained-point limit"
            )

        archive = StoredArchive(
            run_id=row["run_id"],
            start_minute_utc=row["start_minute_utc"],
            minute_count=row["minute_count"],
            band_low_tenths_c=row["band_low_tenths_c"],
            band_high_tenths_c=row["band_high_tenths_c"],
            tolerance_tenths_c=row["tolerance_tenths_c"],
            max_retained_points=row["max_retained_points"],
            retained_count=row["retained_count"],
            retained_points=tuple(retained),
            missing_ranges=tuple(missing),
            payload_sha256=row["payload_sha256"],
            planner_version=row["planner_version"],
            created_at_utc=row["created_at_utc"],
        )
        self._validate_structure(archive)
        return archive

    @staticmethod
    def _validate_structure(archive: StoredArchive) -> None:
        """Check stored anchors and gaps against each other."""
        if archive.minute_count <= 0 or archive.minute_count > MAX_RUN_MINUTES:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has an invalid minute count"
            )
        if archive.band_low_tenths_c > archive.band_high_tenths_c:
            raise ArchiveCorrupt(f"archive {archive.run_id!r} has an inverted band")
        try:
            spans = archive.spans
        except PlanError as exc:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has inconsistent missing ranges"
            ) from exc
        if not spans:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has no measured minutes"
            )
        anchors: dict[int, int] = {}
        for offset, value in archive.retained_points:
            if offset < 0 or offset >= archive.minute_count:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} has an out-of-range point"
                )
            if offset in anchors and anchors[offset] != value:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} has conflicting duplicate points"
                )
            anchors[offset] = value
        for span_start, span_end in spans:
            if span_start not in anchors or span_end - 1 not in anchors:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} does not retain the endpoints "
                    f"of span {span_start}..{span_end - 1}"
                )
        if len(anchors) != archive.retained_count:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has duplicate retained offsets"
            )
        # Reconstruction is bounded and deterministic; this also proves the
        # stored anchors can actually rebuild the whole run.
        reconstruct_spans(spans, anchors, archive.minute_count)

    def get(self, run_id: str) -> Optional[StoredArchive]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM furnace_archives WHERE run_id = ?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(f"failed to read archive: {exc}") from exc
        finally:
            connection.close()
        if row is None:
            return None
        return self._row_to_archive(row)

    def list_recent(self, limit: int = 50) -> list[StoredArchive]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM furnace_archives ORDER BY created_at_utc DESC, "
                "run_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(f"failed to list archives: {exc}") from exc
        finally:
            connection.close()
        return [self._row_to_archive(row) for row in rows]
