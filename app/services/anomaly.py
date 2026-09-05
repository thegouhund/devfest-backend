"""Deteksi anomali statistik (PRD FR-3.2, FR-3.3).

Pembacaan baru dibandingkan terhadap baseline personal; simpangan yang
melewati ambang ditandai sebagai anomali.

Dua lapis yang sengaja dipisah:

- `evaluate_reading` — perhitungan murni, tanpa database. Ini yang diganti
  kalau nanti beralih ke model ML (PRD FR-3.4), tanpa menyentuh skema.
- `detect` — mengambil baseline, memanggil evaluasi, lalu menyimpan hasilnya
  beserta konteks aktivitas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import ActivityLog, Anomaly, Baseline, VitalsReading


# Seberapa jauh ke belakang aktivitas dicari sebagai kemungkinan penyebab.
# Tanpa batas ini, kopi tiga hari lalu akan dikaitkan dengan lonjakan hari
# ini — penjelasan yang menyesatkan. Empat jam kira-kira lama efek kafein
# dan olahraga terhadap detak jantung.
# ponytail: satu jendela untuk semua kategori. Tidur berpengaruh jauh lebih
# lama dari kopi; pisahkan per kategori kalau kaitannya sering meleset.
ACTIVITY_CONTEXT_WINDOW = timedelta(hours=4)

# Pemetaan skor deviasi ke tingkat keparahan (ERD §2.9).
SEVERITY_THRESHOLDS = {
    "medium": 3.0,
    "high": 4.0,
}

# Catatan tingkat alert palsu (diukur, bukan perkiraan):
#
# Simulasi 365 hari pengukuran orang sehat (HR 70, variasi alami ±5, galat
# rPPG ±3) pada ambang z=2.0 menghasilkan 21 alert palsu — sekitar satu
# tiap 17 hari. Ini sifat bawaan z-score, bukan bug: ambang 2.0 memang
# menyisakan ~5% ekor sebaran normal.
#
# Kejadian nyata tetap tertangkap: kopi 3 gelas -> low, demam -> medium,
# takikardia dan bradikardia -> high.
#
# PRD §11 menyebut alert fatigue sebagai risiko dan PRD §13 masih membuka
# penyetelan ambang per metrik. Perbandingan dari 20 orang simulasi:
#
#   ambang 2.0 -> 1 alert palsu tiap ~23 hari, lonjakan sekelas kopi masih terdeteksi
#   ambang 2.5 -> 1 alert palsu tiap ~83 hari, lonjakan kopi tidak lagi terdeteksi
#   ambang 3.0 -> 1 alert palsu tiap ~400 hari, hanya kejadian besar tertangkap
#
# Dipilih 2.0 karena tujuan produk mencakup melihat kaitan gaya hidup dengan
# vital sign (PRD tujuan 3) — kehilangan efek kopi menghilangkan sebagian
# nilai itu. Naikkan lewat ANOMALY_ZSCORE_THRESHOLD kalau uji lapangan
# menunjukkan alert terlalu sering.


@dataclass(frozen=True)
class Detection:
    """Hasil evaluasi satu pembacaan. Sengaja objek biasa, bukan model ORM,
    supaya lapisan perhitungan tidak terikat database."""

    observed: float
    mean: float
    stddev: float
    deviation_score: float
    severity: str


def evaluate_reading(
    observed: float, mean: float, stddev: float, threshold: float
) -> Detection | None:
    """Bandingkan satu nilai terhadap baseline.

    Mengembalikan None kalau masih dalam batas wajar. Simpangan ke bawah
    diperlakukan sama seriusnya dengan ke atas — detak jantung yang terlalu
    rendah juga kondisi yang perlu diperhatikan.
    """
    if stddev <= 0:
        # Baseline sudah punya lantai stddev, tapi pemanggil lain bisa saja
        # mengirim nol. Diam lebih baik daripada z tak hingga.
        return None

    deviation = abs(observed - mean) / stddev
    if deviation <= threshold:
        return None

    return Detection(
        observed=observed,
        mean=mean,
        stddev=stddev,
        deviation_score=deviation,
        severity=classify_severity(deviation),
    )


def classify_severity(deviation_score: float) -> str:
    if deviation_score >= SEVERITY_THRESHOLDS["high"]:
        return "high"
    if deviation_score >= SEVERITY_THRESHOLDS["medium"]:
        return "medium"
    return "low"


def detect(db: Session, reading: VitalsReading) -> list[Anomaly]:
    """Periksa satu pembacaan, simpan anomali kalau ada.

    Mengembalikan daftar kosong kalau belum ada baseline aktif — selama
    masa cold-start sistem sengaja diam (PRD A3), bukan error dan bukan
    pula membanjiri alert dari data yang belum cukup.

    Pemanggil yang melakukan `commit`.
    """
    baseline = _active_baseline(db, reading.user_id, reading.metric_type)
    if baseline is None:
        return []

    detection = evaluate_reading(
        observed=float(reading.value),
        mean=float(baseline.mean_value),
        stddev=float(baseline.stddev_value),
        threshold=get_settings().anomaly_zscore_threshold,
    )
    if detection is None:
        return []

    anomaly = Anomaly(
        user_id=reading.user_id,
        measurement_session_id=reading.measurement_session_id,
        related_activity_id=_nearest_activity_id(db, reading),
        metric_type=reading.metric_type,
        # Nilai baseline disalin, bukan direferensikan: riwayat anomali
        # harus tetap terbaca apa adanya walau baseline berubah nanti.
        observed_value=detection.observed,
        baseline_mean=detection.mean,
        baseline_stddev=detection.stddev,
        deviation_score=detection.deviation_score,
        severity=detection.severity,
        status="new",
        detected_at=reading.recorded_at,
    )
    db.add(anomaly)
    db.flush()
    return [anomaly]


def detect_for_session(db: Session, session_id: uuid.UUID) -> list[Anomaly]:
    """Periksa seluruh pembacaan dari satu sesi pengukuran."""
    readings = (
        db.execute(
            select(VitalsReading).where(
                VitalsReading.measurement_session_id == session_id
            )
        )
        .scalars()
        .all()
    )

    anomalies: list[Anomaly] = []
    for reading in readings:
        anomalies.extend(detect(db, reading))
    return anomalies


def _active_baseline(
    db: Session, user_id: uuid.UUID, metric_type: str
) -> Baseline | None:
    """Baseline terbaru milik user ini yang sudah melewati cold-start.

    Difilter per user: baseline bersifat personal, memakai milik orang lain
    berarti membandingkan seseorang dengan tubuh orang lain.
    """
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


def _nearest_activity_id(db: Session, reading: VitalsReading) -> uuid.UUID | None:
    """Aktivitas milik user yang paling dekat waktunya, dalam jendela terbatas.

    Hanya melihat ke belakang: aktivitas setelah pengukuran tidak mungkin
    jadi penyebabnya.
    """
    recorded_at = reading.recorded_at
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)

    return db.execute(
        select(ActivityLog.id)
        .where(
            ActivityLog.user_id == reading.user_id,
            ActivityLog.occurred_at <= recorded_at,
            ActivityLog.occurred_at >= recorded_at - ACTIVITY_CONTEXT_WINDOW,
        )
        .order_by(ActivityLog.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
