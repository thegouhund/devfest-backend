"""Deteksi anomali vital sign lewat model ML (PRD FR-3.2 s.d. FR-3.4).

Model Isolation Forest (dilatih di devfest-ml, lihat app/ml/anomaly_model.py)
menggantikan pendekatan z-score per-metrik sebelumnya. Bedanya penting:
model ini melihat detak jantung DAN laju napas SEKALIGUS dalam satu
keputusan, bukan menilai tiap metrik sendiri-sendiri — makanya evaluasi
dilakukan per SESI pengukuran, bukan per pembacaan seperti dulu.

Dua lapis yang sengaja dipisah, pola yang sama dengan sebelumnya:

- `evaluate_session` — perhitungan fitur + panggilan model, tanpa efek
  samping database selain baca. Titik ganti kalau modelnya di-retrain atau
  diganti algoritma lain.
- `detect_for_session` — orkestrasi: ambil baseline & bacaan, panggil
  evaluasi, simpan hasilnya beserta konteks aktivitas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ActivityLog, Anomaly, MeasurementSession, VitalsReading
from app.ml.anomaly_model import predict
from app.services.statistics import active_baseline


# Seberapa jauh ke belakang aktivitas dicari sebagai kemungkinan penyebab
# maupun sebagai sumber activity_level_score. Tanpa batas ini, kopi tiga
# hari lalu akan dikaitkan dengan lonjakan hari ini — penjelasan yang
# menyesatkan. Empat jam kira-kira lama efek kafein dan olahraga terhadap
# detak jantung.
# ponytail: satu jendela untuk semua kategori. Tidur berpengaruh jauh lebih
# lama dari kopi; pisahkan per kategori kalau kaitannya sering meleset.
ACTIVITY_CONTEXT_WINDOW = timedelta(hours=4)

# Skor 0-3 dari devfest-ml: seberapa besar aktivitas fisik memengaruhi
# detak jantung, dipakai model supaya lonjakan BPM saat olahraga tidak
# disalahartikan sebagai anomali medis. Diturunkan dari kategori
# ActivityLog terdekat sebelum pengukuran — baik dicatat lewat tombol
# quick-menu maupun lewat chatbot, keduanya masuk tabel yang sama.
#
# ponytail: pemetaan kasar per kategori, bukan dari intensitas/durasi
# aktual. Perhalus kalau chatbot mulai mengekstrak intensitas eksplisit
# dari cerita user (mis. "lari 5km" vs "jalan santai").
ACTIVITY_LEVEL_SCORE = {
    "exercise": 3,
    "smoking": 1,
    "alcohol": 1,
    "coffee": 1,
    "meal": 1,
    "sleep": 0,
    "other": 0,
}
DEFAULT_ACTIVITY_LEVEL_SCORE = 0

# devfest-ml tidak punya sinyal untuk variasi BPM DALAM satu sesi
# pengukuran — open-rppg hanya mengembalikan satu angka HR per sesi, bukan
# rangkaian waktu. Diisi 0 (netral) sampai ada sumber datanya.
# ponytail: kalau open-rppg nanti mengekspos rangkaian BPM per frame,
# hitung stddev-nya di sini alih-alih hardcode 0.
DEFAULT_BPM_VARIANCE = 0.0

# Pemetaan skor anomali model ke tingkat keparahan (ERD §2.9). Ambang model
# (`threshold`) adalah batas anomali/tidak; kelipatannya dipakai sebagai
# proksi seberapa jauh dari batas itu — mekanisme yang sama seperti z-score
# lama, hanya skalanya beda karena skor model bukan unit standar deviasi.
SEVERITY_MULTIPLIER = {
    "medium": 1.5,
    "high": 2.5,
}


@dataclass(frozen=True)
class Detection:
    """Hasil evaluasi satu sesi. Sengaja objek biasa, bukan model ORM,
    supaya lapisan perhitungan tidak terikat database."""

    observed_bpm: float
    baseline_mean_bpm: float
    baseline_stddev_bpm: float
    deviation_score: float
    severity: str


def evaluate_session(
    *,
    heart_rate: float,
    respiration_rate: float,
    baseline_mean_bpm: float,
    baseline_stddev_bpm: float,
    activity_level_score: int,
    delta_rr: float = 0.0,
    bpm_variance: float = DEFAULT_BPM_VARIANCE,
) -> Detection | None:
    """Bandingkan satu sesi pengukuran terhadap model ML.

    Mengembalikan None kalau model menilai wajar. `respiration_rate` == 0
    dihindari sebagai pembagi lewat lantai kecil, bukan exception —
    pembacaan rPPG yang gagal mengukur napas tidak boleh menjatuhkan
    seluruh pipeline deteksi.

    `delta_rr` diterima sudah terhitung (bukan `baseline_mean_rr`) supaya
    pemanggil yang menentukan artinya "tidak ada baseline respirasi" —
    default 0.0 di sini berarti "anggap tidak ada deviasi napas", bukan
    "baseline-nya nol". Membedakan keduanya penting: kalau fungsi ini yang
    menghitung `respiration_rate - 0`, baseline yang belum lewat cold-start
    akan terbaca sebagai lonjakan napas belasan poin — false alarm besar.
    """
    delta_bpm = heart_rate - baseline_mean_bpm
    bpm_to_rr_ratio = heart_rate / max(respiration_rate, 1.0)

    result = predict(
        {
            "delta_bpm": delta_bpm,
            "delta_rr": delta_rr,
            "bpm_to_rr_ratio": bpm_to_rr_ratio,
            "bpm_variance": bpm_variance,
            "activity_level_score": activity_level_score,
        }
    )
    if not result.is_anomaly:
        return None

    return Detection(
        observed_bpm=heart_rate,
        baseline_mean_bpm=baseline_mean_bpm,
        baseline_stddev_bpm=baseline_stddev_bpm,
        deviation_score=result.score,
        severity=classify_severity(result.score, result.threshold),
    )


def classify_severity(score: float, threshold: float) -> str:
    if score >= threshold * SEVERITY_MULTIPLIER["high"]:
        return "high"
    if score >= threshold * SEVERITY_MULTIPLIER["medium"]:
        return "medium"
    return "low"


def detect_for_session(db: Session, session_id: uuid.UUID) -> list[Anomaly]:
    """Periksa satu sesi pengukuran, simpan anomali kalau model mendeteksi.

    Mengembalikan daftar kosong kalau belum ada baseline heart_rate aktif
    — selama masa cold-start sistem sengaja diam (PRD A3), bukan error dan
    bukan pula membanjiri alert dari data yang belum cukup. Laju napas
    tanpa baseline aktif tetap boleh jalan dengan baseline mean 0 (model
    lebih peka ke delta_bpm; hilangnya sinyal delta_rr bukan alasan diam
    total).

    Pemanggil yang melakukan `commit`.
    """
    session = db.get(MeasurementSession, session_id)
    if session is None:
        return []

    readings = {
        reading.metric_type: reading
        for reading in db.execute(
            select(VitalsReading).where(
                VitalsReading.measurement_session_id == session_id
            )
        ).scalars()
    }
    heart_rate_reading = readings.get("heart_rate")
    if heart_rate_reading is None:
        return []

    hr_baseline = active_baseline(db, session.family_member_id, "heart_rate")
    if hr_baseline is None:
        return []

    rr_baseline = active_baseline(db, session.family_member_id, "respiration_rate")
    respiration_reading = readings.get("respiration_rate")

    respiration_rate = (
        float(respiration_reading.value) if respiration_reading else 0.0
    )
    # delta_rr hanya bermakna kalau KEDUANYA ada. Baseline napas yang belum
    # lewat cold-start bukan "baseline nol" — memperlakukannya begitu
    # membuat napas normal (mis. 16/menit) terbaca sebagai lonjakan besar.
    delta_rr = (
        respiration_rate - float(rr_baseline.mean_value)
        if rr_baseline is not None and respiration_reading is not None
        else 0.0
    )

    detection = evaluate_session(
        heart_rate=float(heart_rate_reading.value),
        respiration_rate=respiration_rate,
        baseline_mean_bpm=float(hr_baseline.mean_value),
        baseline_stddev_bpm=float(hr_baseline.stddev_value),
        delta_rr=delta_rr,
        activity_level_score=_activity_level_score(db, session),
    )
    if detection is None:
        return []

    anomaly = Anomaly(
        family_member_id=session.family_member_id,
        measurement_session_id=session.id,
        related_activity_id=_nearest_activity_id(
            db, session.family_member_id, heart_rate_reading.recorded_at
        ),
        metric_type="heart_rate",
        # Nilai baseline disalin, bukan direferensikan: riwayat anomali
        # harus tetap terbaca apa adanya walau baseline berubah nanti.
        observed_value=detection.observed_bpm,
        baseline_mean=detection.baseline_mean_bpm,
        baseline_stddev=detection.baseline_stddev_bpm,
        deviation_score=detection.deviation_score,
        severity=detection.severity,
        status="new",
        detected_at=heart_rate_reading.recorded_at,
    )
    db.add(anomaly)
    db.flush()
    return [anomaly]


def _activity_level_score(db: Session, session: MeasurementSession) -> int:
    """Skor 0-3 dari kategori aktivitas terdekat sebelum sesi ini dimulai."""
    activity_id = _nearest_activity_id(
        db, session.family_member_id, session.started_at
    )
    if activity_id is None:
        return DEFAULT_ACTIVITY_LEVEL_SCORE

    category = db.execute(
        select(ActivityLog.category).where(ActivityLog.id == activity_id)
    ).scalar_one()
    return ACTIVITY_LEVEL_SCORE.get(category, DEFAULT_ACTIVITY_LEVEL_SCORE)


def _nearest_activity_id(
    db: Session, family_member_id: uuid.UUID, reference_time
) -> uuid.UUID | None:
    """Aktivitas milik profil yang paling dekat waktunya, dalam jendela terbatas.

    Hanya melihat ke belakang: aktivitas setelah pengukuran tidak mungkin
    jadi penyebabnya.
    """
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=UTC)

    return db.execute(
        select(ActivityLog.id)
        .where(
            ActivityLog.family_member_id == family_member_id,
            ActivityLog.occurred_at <= reference_time,
            ActivityLog.occurred_at >= reference_time - ACTIVITY_CONTEXT_WINDOW,
        )
        .order_by(ActivityLog.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()
