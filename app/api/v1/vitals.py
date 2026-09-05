"""Endpoint statistik vital sign (PRD FR-2.1, FR-2.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas import (
    BaselineSummary,
    MetricSummary,
    PeriodComparison,
    PeriodRange,
    SummaryResponse,
    TrendBucket,
    TrendResponse,
)
from app.services import statistics
from app.services.visibility import accessible_user_ids


router = APIRouter()

# Metrik yang diringkas di endpoint summary. Diambil dari seed metric_types
# supaya penambahan metrik tidak perlu menyentuh kode ini.
SUMMARY_METRICS = ("heart_rate", "hrv_rmssd", "respiration_rate")


def _resolve_scope(
    db: Session, viewer: User, user_id: uuid.UUID | None
) -> set[uuid.UUID]:
    """Tentukan user mana yang datanya boleh diagregasi.

    Tanpa `user_id`, lingkupnya diri sendiri. Dengan `user_id`, aksesnya
    diperiksa dulu — meminta data yang tidak boleh dilihat menghasilkan 403
    utuh, bukan hasil kosong yang terbaca seolah orangnya belum mengukur.
    """
    visible = accessible_user_ids(db, viewer.id, "vitals")

    if user_id is None:
        return {viewer.id}

    if user_id not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Anda tidak punya akses ke data user ini",
        )
    return {user_id}


def _validate_range(start: datetime, end: datetime) -> None:
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parameter `end` harus setelah `start`",
        )


def _validate_metric(db: Session, metric_type: str) -> None:
    if not statistics.metric_exists(db, metric_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Metrik '{metric_type}' tidak dikenal",
        )


@router.get("/trend", response_model=TrendResponse)
def read_trend(
    metric_type: str = Query(...),
    start: datetime = Query(...),
    end: datetime = Query(...),
    bucket: Literal["day", "week", "month"] = Query(default="day"),
    user_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrendResponse:
    """Tren satu metrik dalam rentang waktu (FR-2.1).

    Bucket tanpa data tidak dikirim — grafik menampilkan celah, bukan nol.
    """
    _validate_metric(db, metric_type)
    _validate_range(start, end)
    scope = _resolve_scope(db, current_user, user_id)

    buckets = statistics.trend(db, scope, metric_type, start, end, bucket)

    return TrendResponse(
        metric_type=metric_type,
        unit=statistics.metric_unit(db, metric_type),
        buckets=[TrendBucket(**b) for b in buckets],
    )


@router.get("/summary", response_model=SummaryResponse)
def read_summary(
    start: datetime = Query(...),
    end: datetime = Query(...),
    user_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SummaryResponse:
    """Ringkasan seluruh metrik: rata-rata, min/maks, baseline, dan
    perbandingan dengan periode sebelumnya (FR-2.2)."""
    _validate_range(start, end)
    scope = _resolve_scope(db, current_user, user_id)
    subject_id = next(iter(scope))

    previous_start, previous_end = statistics.previous_period(start, end)
    metrics: list[MetricSummary] = []

    for metric_type in SUMMARY_METRICS:
        current = statistics.aggregate(db, scope, metric_type, start, end)
        if current is None:
            continue

        baseline = statistics.active_baseline(db, subject_id, metric_type)
        previous = statistics.aggregate(
            db, scope, metric_type, previous_start, previous_end
        )

        metrics.append(
            MetricSummary(
                metric_type=metric_type,
                unit=statistics.metric_unit(db, metric_type),
                **current,
                baseline=(
                    BaselineSummary(
                        mean=float(baseline.mean_value),
                        stddev=float(baseline.stddev_value),
                        is_active=baseline.is_active,
                    )
                    if baseline
                    else None
                ),
                previous_period=(
                    PeriodComparison(
                        avg=previous["avg"],
                        change_percent=statistics.change_percent(
                            current["avg"], previous["avg"]
                        ),
                    )
                    if previous
                    else None
                ),
            )
        )

    return SummaryResponse(
        period=PeriodRange(start=start, end=end), metrics=metrics
    )
