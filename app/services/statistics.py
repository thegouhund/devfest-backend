"""Agregasi vital sign untuk dashboard (PRD FR-2.1 s.d. FR-2.4).

Setiap fungsi di sini menerima himpunan user id yang sudah difilter
`accessible_user_ids` — bukan menerima id mentah lalu memfilter sendiri.
Dengan begitu tidak ada jalur yang bisa lupa mengecek privasi.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import Baseline, MetricType, VitalsReading


# Ukuran bucket yang didukung untuk grafik tren.
BUCKET_SIZES = ("day", "week", "month")


def metric_exists(db: Session, metric_type: str) -> bool:
    """Validasi lewat tabel lookup, bukan daftar hardcode — menambah metrik
    cukup satu INSERT (ERD note 2)."""
    return db.get(MetricType, metric_type) is not None


def metric_unit(db: Session, metric_type: str) -> str | None:
    metric = db.get(MetricType, metric_type)
    return metric.default_unit if metric else None


def trend(
    db: Session,
    user_ids: set[uuid.UUID],
    metric_type: str,
    start: datetime,
    end: datetime,
    bucket: str = "day",
) -> list[dict]:
    """Rata-rata, min, maks, dan jumlah pembacaan per rentang waktu.

    Bucket tanpa data tidak dikembalikan — deretnya berlubang, dan itu
    memang kontraknya: frontend menampilkan celah, bukan nol palsu.
    """
    if not user_ids:
        return []

    bucket_column = _bucket_expression(db, bucket)

    rows = db.execute(
        select(
            bucket_column.label("bucket"),
            func.avg(VitalsReading.value).label("avg"),
            func.min(VitalsReading.value).label("min"),
            func.max(VitalsReading.value).label("max"),
            func.count(VitalsReading.id).label("count"),
        )
        .where(
            VitalsReading.user_id.in_(user_ids),
            VitalsReading.metric_type == metric_type,
            VitalsReading.recorded_at >= start,
            VitalsReading.recorded_at <= end,
        )
        .group_by(bucket_column)
        .order_by(bucket_column)
    ).all()

    return [
        {
            "bucket": row.bucket,
            "avg": float(row.avg),
            "min": float(row.min),
            "max": float(row.max),
            "count": row.count,
        }
        for row in rows
    ]


def _bucket_expression(db: Session, bucket: str):
    """Ekspresi pemotongan waktu sesuai dialect database."""
    if db.bind.dialect.name == "postgresql":
        return func.date_trunc(bucket, VitalsReading.recorded_at)

    # SQLite tidak punya date_trunc; strftime menghasilkan string yang
    # tetap urut secara leksikografis, jadi ORDER BY tetap benar.
    formats = {
        "day": "%Y-%m-%d 00:00:00",
        "week": "%Y-%W",
        "month": "%Y-%m-01 00:00:00",
    }
    return func.strftime(formats[bucket], VitalsReading.recorded_at)


def aggregate(
    db: Session,
    user_ids: set[uuid.UUID],
    metric_type: str,
    start: datetime,
    end: datetime,
) -> dict | None:
    """Ringkasan satu metrik pada satu rentang. None kalau tidak ada data."""
    if not user_ids:
        return None

    row = db.execute(
        select(
            func.avg(VitalsReading.value).label("avg"),
            func.min(VitalsReading.value).label("min"),
            func.max(VitalsReading.value).label("max"),
            func.count(VitalsReading.id).label("count"),
        ).where(
            VitalsReading.user_id.in_(user_ids),
            VitalsReading.metric_type == metric_type,
            VitalsReading.recorded_at >= start,
            VitalsReading.recorded_at <= end,
        )
    ).one()

    if not row.count:
        return None
    return {
        "avg": float(row.avg),
        "min": float(row.min),
        "max": float(row.max),
        "count": row.count,
    }


def active_baseline(
    db: Session, user_id: uuid.UUID, metric_type: str
) -> Baseline | None:
    """Baseline terbaru yang sudah aktif (lewat masa cold-start)."""
    return db.execute(
        select(Baseline)
        .where(
            Baseline.user_id == user_id,
            Baseline.metric_type == metric_type,
            Baseline.is_active.is_(True),
        )
        .order_by(Baseline.window_end.desc())
        .limit(1)
    ).scalar_one_or_none()


def previous_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Rentang sebelumnya dengan panjang yang sama, untuk perbandingan."""
    span = end - start
    return start - span, start


def change_percent(current: float, previous: float) -> float | None:
    """Perubahan relatif dalam persen. None kalau pembanding nol."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def latest_readings(db: Session, user_id: uuid.UUID) -> list[VitalsReading]:
    """Pembacaan terakhir per metrik, untuk kartu ringkas di dashboard."""
    newest = (
        select(
            VitalsReading.metric_type,
            func.max(VitalsReading.recorded_at).label("recorded_at"),
        )
        .where(VitalsReading.user_id == user_id)
        .group_by(VitalsReading.metric_type)
        .subquery()
    )

    return (
        db.execute(
            select(VitalsReading).join(
                newest,
                (VitalsReading.metric_type == newest.c.metric_type)
                & (VitalsReading.recorded_at == newest.c.recorded_at),
            ).where(VitalsReading.user_id == user_id)
        )
        .scalars()
        .all()
    )


def last_measurement_at(db: Session, user_id: uuid.UUID) -> datetime | None:
    return db.execute(
        select(func.max(VitalsReading.recorded_at)).where(
            VitalsReading.user_id == user_id
        )
    ).scalar_one_or_none()
