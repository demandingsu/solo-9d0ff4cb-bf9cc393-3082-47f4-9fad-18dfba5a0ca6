"""FastAPI application for the furnace cold archive.

Endpoints
---------
* ``POST /api/v1/archives`` — compress one run and store it (all-or-nothing).
* ``GET  /api/v1/archives`` — list archived runs.
* ``GET  /api/v1/archives/{run_id}`` — archive metadata.
* ``GET  /api/v1/archives/{run_id}/minutes`` — reconstructed values and in-band
  periods for a minute range of one run.
* ``GET  /health`` — database reachability.

A failed or rejected write never affects archives that are already stored;
queries always read the same stored archive and derive everything from it.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__, db
from .models import (
    ArchiveCreateRequest,
    ArchiveCreatedResponse,
    ArchiveListItem,
    ArchiveListResponse,
    ArchiveQueryResponse,
    ArchiveSummary,
    BandInterval,
    ErrorResponse,
    HealthResponse,
    MinuteSample,
)
from .planner import (
    PlanError,
    classify,
    in_band_intervals,
    plan_archive,
    reconstruct_spans,
)


def _store_path() -> str:
    return os.environ.get("FURNACE_DB_PATH", db.DEFAULT_DB_PATH)


def _summary(archive: db.StoredArchive) -> ArchiveSummary:
    measured = sum(end - start for start, end in archive.spans)
    return ArchiveSummary(
        run_id=archive.run_id,
        start_minute_utc=archive.start_minute_utc,
        end_minute_utc=archive.end_minute_utc,
        minute_count=archive.minute_count,
        band_low_tenths_c=archive.band_low_tenths_c,
        band_high_tenths_c=archive.band_high_tenths_c,
        tolerance_tenths_c=archive.tolerance_tenths_c,
        max_retained_points=archive.max_retained_points,
        retained_points_count=archive.retained_count,
        measured_minutes=measured,
        missing_minutes=archive.minute_count - measured,
        segment_count=len(archive.spans),
        payload_sha256=archive.payload_sha256,
        planner_version=archive.planner_version,
        created_at_utc=archive.created_at_utc,
    )


def _error(
    status_code: int,
    reason_code: str,
    detail: str,
    *,
    retryable: bool = False,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> JSONResponse:
    body = ErrorResponse(
        detail=detail,
        reason_code=reason_code,
        retryable=retryable,
        minimum_retained_points=minimum,
        max_retained_points=maximum,
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


class ApiError(HTTPException):
    """An HTTP error carrying a stable reason code."""

    def __init__(
        self,
        status_code: int,
        reason_code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.reason_code = reason_code
        self.retryable = retryable


def _load_archive(store: db.ArchiveStore, run_id: str) -> db.StoredArchive:
    try:
        archive = store.get(run_id)
    except db.ArchiveCorrupt as exc:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "archive_corrupt",
            f"stored archive is unreadable: {exc}",
        ) from exc
    except db.ArchiveUnavailable as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "archive_unavailable",
            str(exc),
            retryable=True,
        ) from exc
    if archive is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "archive_not_found",
            f"run {run_id!r} has no archive",
        )
    return archive


def _archive_values(archive: db.StoredArchive) -> list[Optional[int]]:
    try:
        return reconstruct_spans(
            archive.spans, dict(archive.retained_points), archive.minute_count
        )
    except PlanError as exc:  # pragma: no cover - guarded at insert time
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "archive_corrupt",
            f"stored archive cannot be reconstructed: {exc}",
        ) from exc


def _band_state(value: int, low: int, high: int) -> str:
    return {-1: "below", 0: "in_band", 1: "above"}[classify(value, low, high)]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.startup_error = None
    try:
        db.ArchiveStore(_store_path()).init()
    except (OSError, sqlite3.Error, db.ArchiveUnavailable) as exc:
        app.state.startup_error = str(exc)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Furnace Cold Archive",
        version=__version__,
        lifespan=_lifespan,
        description=(
            "Compress minute-by-minute furnace temperature records into a "
            "bounded-retention cold archive, then query reconstructed values "
            "and continuous in-band holding periods. All temperatures and "
            "tolerances are integers in tenths of a degree Celsius; band limits "
            "are inclusive. Missing samples are never bridged."
        ),
    )
    api = APIRouter(prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status_code, exc.reason_code, str(exc.detail),
                      retryable=exc.retryable)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        return _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_request",
            f"request validation failed: {problems}",
        )

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["ops"],
        summary="Report database reachability",
    )
    def health() -> JSONResponse:
        store = db.ArchiveStore(_store_path())
        try:
            store.init()
            ok = store.healthy()
        except (OSError, sqlite3.Error, db.ArchiveUnavailable):
            ok = False
        body = HealthResponse(
            status="ok" if ok else "degraded",
            database="ok" if ok else "unavailable",
            schema_version=db.SCHEMA_VERSION,
        )
        return JSONResponse(status_code=200 if ok else 503, content=body.model_dump())

    @api.post(
        "/archives",
        response_model=ArchiveCreatedResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            409: {"model": ErrorResponse, "description": "Run already archived"},
            422: {"model": ErrorResponse, "description": "No feasible plan"},
            503: {"model": ErrorResponse, "description": "Archive write failed"},
        },
        tags=["archive"],
        summary="Compress a furnace run and store it in the cold archive",
    )
    def create_archive(payload: ArchiveCreateRequest) -> JSONResponse:
        store = db.ArchiveStore(_store_path())
        readings: list[Optional[int]] = list(payload.readings)
        try:
            plan = plan_archive(
                readings,
                low=payload.band_low_tenths_c,
                high=payload.band_high_tenths_c,
                tolerance=payload.tolerance_tenths_c,
                max_retained=payload.max_retained_points,
            )
        except PlanError as exc:
            details = exc.details
            return _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "no_feasible_plan",
                exc.message,
                minimum=details.get("minimum_retained_points"),
                maximum=payload.max_retained_points,
            )

        retained_points = [
            (offset, readings[offset]) for offset in plan.retained_offsets
        ]
        try:
            archive = store.insert(
                run_id=payload.run_id,
                start_minute_utc=payload.start_minute_utc,
                minute_count=len(readings),
                band_low_tenths_c=payload.band_low_tenths_c,
                band_high_tenths_c=payload.band_high_tenths_c,
                tolerance_tenths_c=payload.tolerance_tenths_c,
                max_retained_points=payload.max_retained_points,
                retained_points=retained_points,
                missing_ranges=db.missing_ranges_from_readings(readings),
            )
        except db.ArchiveConflict:
            return _error(
                status.HTTP_409_CONFLICT,
                "run_already_archived",
                f"run {payload.run_id!r} is already archived; the existing "
                "archive was left unchanged",
            )
        except db.ArchiveWriteError as exc:
            return _error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "archive_write_failed",
                exc.message,
                retryable=exc.retryable,
            )
        except db.ArchiveUnavailable as exc:
            return _error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "archive_unavailable",
                str(exc),
                retryable=True,
            )

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content=ArchiveCreatedResponse(archive=_summary(archive)).model_dump(),
        )

    @api.get(
        "/archives",
        response_model=ArchiveListResponse,
        tags=["archive"],
        summary="List archived runs, newest first, skipping corrupt archives",
    )
    def list_archives(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> ArchiveListResponse:
        store = db.ArchiveStore(_store_path())
        try:
            result = store.list_recent(limit)
        except db.ArchiveUnavailable as exc:
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "archive_unavailable",
                str(exc),
                retryable=True,
            ) from exc
        return ArchiveListResponse(
            archives=[
                ArchiveListItem(
                    run_id=item.run_id,
                    start_minute_utc=item.start_minute_utc,
                    end_minute_utc=item.end_minute_utc,
                    minute_count=item.minute_count,
                    retained_points_count=item.retained_count,
                    created_at_utc=item.created_at_utc,
                )
                for item in result.archives
            ],
            skipped_corrupt_run_ids=list(result.skipped_run_ids),
            limit=result.limit,
            available_exhausted=result.exhausted,
        )

    @api.get(
        "/archives/{run_id}",
        response_model=ArchiveSummary,
        tags=["archive"],
        summary="Archive metadata and retained-point count",
    )
    def get_archive(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> ArchiveSummary:
        return _summary(_load_archive(db.ArchiveStore(_store_path()), run_id))

    @api.get(
        "/archives/{run_id}/minutes",
        response_model=ArchiveQueryResponse,
        responses={
            404: {"model": ErrorResponse, "description": "Run not archived"},
            416: {"model": ErrorResponse, "description": "Range outside the run"},
        },
        tags=["archive"],
        summary="Reconstructed temperatures and in-band periods for a minute range",
    )
    def query_minutes(
        run_id: Annotated[str, Path(min_length=1, max_length=64)],
        start_minute_utc: Annotated[
            int, Query(ge=0, description="First minute to return, inclusive")
        ],
        end_minute_utc: Annotated[
            int, Query(ge=0, description="Last minute to return, inclusive")
        ],
    ) -> ArchiveQueryResponse:
        if start_minute_utc > end_minute_utc:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_range",
                "start_minute_utc must not be later than end_minute_utc",
            )
        archive = _load_archive(db.ArchiveStore(_store_path()), run_id)
        if (
            start_minute_utc < archive.start_minute_utc
            or end_minute_utc > archive.end_minute_utc
        ):
            raise ApiError(
                status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                "range_outside_run",
                f"run {run_id!r} covers minutes "
                f"{archive.start_minute_utc}..{archive.end_minute_utc} inclusive; "
                f"requested {start_minute_utc}..{end_minute_utc}",
            )

        values = _archive_values(archive)
        offset_start = start_minute_utc - archive.start_minute_utc
        offset_end = end_minute_utc - archive.start_minute_utc + 1
        window = values[offset_start:offset_end]

        minutes: list[MinuteSample] = []
        missing_minutes: list[int] = []
        for index, value in enumerate(window):
            minute = start_minute_utc + index
            if value is None:
                missing_minutes.append(minute)
                minutes.append(
                    MinuteSample(
                        minute_utc=minute,
                        value_tenths_c=None,
                        missing=True,
                        band_state="unknown",
                    )
                )
                continue
            minutes.append(
                MinuteSample(
                    minute_utc=minute,
                    value_tenths_c=value,
                    missing=False,
                    band_state=_band_state(
                        value, archive.band_low_tenths_c, archive.band_high_tenths_c
                    ),
                )
            )

        # In-band periods are a property of the whole archived run, so compute
        # them across the run and return those overlapping the requested range.
        full_intervals = in_band_intervals(
            values,
            archive.band_low_tenths_c,
            archive.band_high_tenths_c,
            archive.start_minute_utc,
        )
        intervals = [
            BandInterval(
                start_minute_utc=item["start_minute"],
                end_minute_utc=item["end_minute"],
                duration_minutes=item["duration_minutes"],
            )
            for item in full_intervals
            if item["start_minute"] <= end_minute_utc
            and item["end_minute"] >= start_minute_utc
        ]

        return ArchiveQueryResponse(
            run_id=archive.run_id,
            start_minute_utc=archive.start_minute_utc,
            end_minute_utc=archive.end_minute_utc,
            band_low_tenths_c=archive.band_low_tenths_c,
            band_high_tenths_c=archive.band_high_tenths_c,
            tolerance_tenths_c=archive.tolerance_tenths_c,
            max_retained_points=archive.max_retained_points,
            retained_points_count=archive.retained_count,
            query_start_minute_utc=start_minute_utc,
            query_end_minute_utc=end_minute_utc,
            minutes=minutes,
            in_band_intervals=intervals,
            missing_minutes=missing_minutes,
            payload_sha256=archive.payload_sha256,
        )

    app.include_router(api)
    return app


app = create_app()
