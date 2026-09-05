"""Baseline statistik personal per metrik (PRD FR-3.1).

Baseline adalah "kondisi normal" seseorang: rata-rata dan simpangan baku
pembacaan terkini. Deteksi anomali membandingkan pembacaan baru terhadap
angka ini, jadi baseline yang salah menghasilkan alert palsu berhari-hari.

Baru dipakai untuk alert setelah masa cold-start terlampaui (PRD A3,
default 14 hari) — data terlalu sedikit membuat "normal" belum terbentuk.
"""

from __future__ import annotations

import statistics as py_statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Baseline, VitalsReading


# Panjang jendela pengamatan. Cukup panjang untuk meredam variasi harian,
# cukup pendek supaya baseline mengikuti perubahan kondisi tubuh.
BASELINE_WINDOW_DAYS = 30

# Minimal pembacaan sebelum baseline layak dihitung sama sekali.
MIN_SAMPLES_FOR_BASELINE = 1

# Lantai simpangan baku. Satu sampel membuat stdev tak terdefinisi, dan
# nilai yang seragam membuatnya nol — keduanya menghasilkan pembagian nol
# saat menghitung z-score, sehingga *setiap* pembacaan berikutnya tampak
# anomali tak hingga.
#
# Besarnya dipilih dari galat alat, bukan sekadar "bukan nol": pengukuran
# rPPG sendiri meleset beberapa bpm (uji Task 9: target 72, terbaca 73.35).
# Dengan ambang z=2.0, lantai 3.0 berarti selisih di bawah ~6 bpm tidak
# dianggap anomali — masih dalam ketidakpastian alat.
#
# ponytail: satu lantai untuk semua metrik. HRV (satuan ms, rentang puluhan)
# dan laju napas (belasan per menit) punya skala berbeda dari HR; pisahkan
# per metrik kalau alert palsu terkumpul di salah satunya.
MIN_STDDEV = 3.0


@dataclass(frozen=True)
class BaselineStats:
    mean: float
    stddev: float
    sample_count: int


def compute_baseline(values: list[float]) -> BaselineStats | None:
    """Hitung rata-rata dan simpangan baku dari deretan nilai.

    Mengembalikan None kalau tidak ada data. Simpangan baku selalu di atas
    `MIN_STDDEV` supaya pembagian z-score tidak pernah meledak.
    """
    if not values:
        return None

    mean = py_statistics.mean(values)
    # stdev butuh minimal dua titik; satu sampel jatuh ke lantai.
    stddev = py_statistics.stdev(values) if len(values) > 1 else 0.0

    return BaselineStats(
        mean=mean,
        stddev=max(stddev, MIN_STDDEV),
        sample_count=len(values),
    )


def recompute_for_user(
    db: Session,
    user_id: uuid.UUID,
    metric_type: str,
    window_end: datetime | None = None,
) -> Baseline | None:
    """Hitung ulang baseline satu user untuk satu metrik.

    Pemanggil yang melakukan `commit`. Mengembalikan None kalau belum ada
    pembacaan sama sekali.
    """
    settings = get_settings()
    window_end = window_end or datetime.now(UTC)
    window_start = window_end - timedelta(days=BASELINE_WINDOW_DAYS)

    values = [
        float(value)
        for value in db.execute(
            select(VitalsReading.value).where(
                VitalsReading.user_id == user_id,
                VitalsReading.metric_type == metric_type,
                VitalsReading.recorded_at >= window_start,
                VitalsReading.recorded_at <= window_end,
            )
        ).scalars()
    ]

    stats = compute_baseline(values)
    if stats is None or stats.sample_count < MIN_SAMPLES_FOR_BASELINE:
        return None

    is_active = _has_enough_history(
        db, user_id, metric_type, window_end, settings.baseline_cold_start_days
    )

    return _upsert(
        db,
        user_id=user_id,
        metric_type=metric_type,
        stats=stats,
        window_start=window_start,
        window_end=window_end,
        is_active=is_active,
    )


def _has_enough_history(
    db: Session,
    user_id: uuid.UUID,
    metric_type: str,
    window_end: datetime,
    cold_start_days: int,
) -> bool:
    """Apakah rentang data sudah melampaui masa cold-start.

    Diukur dari jarak pembacaan tertua ke terbaru, bukan dari jumlah
    pembacaan: 50 pengukuran dalam sehari tidak memberi tahu apa pun
    tentang pola normal seseorang.
    """
    earliest = db.execute(
        select(VitalsReading.recorded_at)
        .where(
            VitalsReading.user_id == user_id,
            VitalsReading.metric_type == metric_type,
        )
        .order_by(VitalsReading.recorded_at)
        .limit(1)
    ).scalar_one_or_none()

    if earliest is None:
        return False

    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)

    span = window_end - earliest
    return span >= timedelta(days=cold_start_days)


def _upsert(
    db: Session,
    *,
    user_id: uuid.UUID,
    metric_type: str,
    stats: BaselineStats,
    window_start: datetime,
    window_end: datetime,
    is_active: bool,
) -> Baseline:
    """Simpan baseline, menimpa snapshot di window yang sama.

    UNIQUE(user_id, metric_type, window_end) melarang baris kedua, jadi
    hitung ulang harus memperbarui baris lama.
    """
    existing = db.execute(
        select(Baseline).where(
            Baseline.user_id == user_id,
            Baseline.metric_type == metric_type,
            Baseline.window_end == window_end,
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = Baseline(
            user_id=user_id,
            metric_type=metric_type,
            window_end=window_end,
        )
        db.add(existing)

    existing.mean_value = stats.mean
    existing.stddev_value = stats.stddev
    existing.sample_count = stats.sample_count
    existing.window_start = window_start
    existing.is_active = is_active
    db.flush()
    return existing


def recompute_all_metrics(db: Session, user_id: uuid.UUID) -> list[Baseline]:
    """Hitung ulang seluruh metrik seorang user.

    Dipanggil setelah sesi pengukuran selesai, supaya baseline selalu
    mencerminkan data terbaru.
    """
    metrics = (
        db.execute(
            select(VitalsReading.metric_type)
            .where(VitalsReading.user_id == user_id)
            .distinct()
        )
        .scalars()
        .all()
    )

    results = []
    for metric_type in metrics:
        baseline = recompute_for_user(db, user_id, metric_type)
        if baseline is not None:
            results.append(baseline)
    return results
