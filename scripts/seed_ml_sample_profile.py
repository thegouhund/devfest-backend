"""Seed satu profil dengan 14 hari riwayat dari data sintetis devfest-ml.

Dipakai untuk mengaktifkan deteksi anomali (perlu >=14 hari data untuk
melewati cold-start, lihat app/services/baseline.py) tanpa menunggu
pengukuran asli terkumpul.

Sumber: devfest-ml/data/health_dummy_data.csv, user_id
3d967c8c-0666-467a-8e55-ac21e799b479, 14 baris pertama. Tiap baris CSV
jadi satu MeasurementSession + VitalsReading (heart_rate, respiration_rate,
hrv_rmssd) bertanggal mundur satu hari per baris, supaya window baseline
30 hari memuat semuanya sekaligus melewati cold-start 14 hari.

Kolom `bpm_variance` di CSV sumber berlabel "HRV Proxy" di notebook riset
devfest-ml (lihat README backend, bagian "Deteksi Anomali (ML)") — dipetakan
ke `hrv_rmssd` di sini supaya konsisten dengan cara app/services/anomaly.py
mengisi fitur `bpm_variance` model dari pembacaan asli.

Cara pakai (di dalam container backend, atau lokal dengan DATABASE_URL
yang sama) — dijalankan sebagai modul (`-m`), bukan file langsung, supaya
import `app.*` bekerja tanpa perlu mengubah PYTHONPATH:

    python -m scripts.seed_ml_sample_profile admin@example.com

    # Di VPS lewat docker compose:
    docker compose exec backend python -m scripts.seed_ml_sample_profile admin@example.com
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from app.db.models import Account, FamilyMember, MeasurementSession, VitalsReading
from app.db.session import SessionLocal
from app.services.baseline import recompute_all_metrics


# 14 baris pertama milik user_id 3d967c8c-0666-467a-8e55-ac21e799b479 di
# devfest-ml/data/health_dummy_data.csv, apa adanya. Ditulis literal (bukan
# dibaca dari CSV saat runtime) supaya skrip ini tidak butuh path relatif ke
# repo devfest-ml yang mungkin tidak ikut ter-deploy di server.
SOURCE_ROWS = [
    # (mean_bpm, bpm_variance -> hrv_rmssd, respiration_rate)
    (84.6, 45.4, 19.0),
    (69.1, 44.6, 17.0),
    (66.1, 46.4, 16.5),
    (67.6, 47.1, 16.9),
    (67.8, 45.1, 15.9),
    (67.0, 51.8, 15.2),
    (67.3, 42.0, 17.7),
    (82.4, 48.1, 16.5),
    (127.9, 25.3, 31.2),
    (66.7, 48.6, 16.4),
    (68.6, 48.4, 16.2),
    (82.7, 49.0, 19.0),
    (65.7, 43.7, 15.9),
    (70.5, 44.6, 17.1),
]

PROFILE_NAME = "Contoh ML (devfest-ml)"
PROFILE_DATE_OF_BIRTH = datetime(1966, 1, 1).date()  # usia 60 di CSV sumber
PROFILE_GENDER = "female"
PROFILE_HEIGHT_CM = 169.0
PROFILE_WEIGHT_KG = 63.3


def main(email: str) -> None:
    with SessionLocal() as db:
        account = db.query(Account).filter(Account.email == email.lower()).one_or_none()
        if account is None:
            print(f"Akun dengan email '{email}' tidak ditemukan.", file=sys.stderr)
            sys.exit(1)

        profile = FamilyMember(
            account_id=account.id,
            full_name=PROFILE_NAME,
            date_of_birth=PROFILE_DATE_OF_BIRTH,
            gender=PROFILE_GENDER,
            height_cm=PROFILE_HEIGHT_CM,
            weight=PROFILE_WEIGHT_KG,
            role="member",
        )
        db.add(profile)
        db.flush()

        now = datetime.now(UTC)
        # Baris pertama = paling baru (kemarin), baris terakhir = 14 hari lalu.
        # Dibalik urutan waktunya supaya insertion order = urutan kronologis,
        # meski tidak berpengaruh ke hasil karena baseline dihitung dari
        # keseluruhan window, bukan urutan baris.
        for offset, (heart_rate, hrv_rmssd, respiration_rate) in enumerate(SOURCE_ROWS):
            moment = now - timedelta(days=len(SOURCE_ROWS) - offset)

            session = MeasurementSession(
                family_member_id=profile.id,
                initiated_by_family_member_id=profile.id,
                capture_method="upload",
                started_at=moment,
                ended_at=moment,
                processing_status="completed",
                signal_quality_flag="good",
                signal_quality_score=0.9,
            )
            db.add(session)
            db.flush()

            db.add(
                VitalsReading(
                    measurement_session_id=session.id,
                    family_member_id=profile.id,
                    recorded_at=moment,
                    metric_type="heart_rate",
                    value=heart_rate,
                    unit="bpm",
                )
            )
            db.add(
                VitalsReading(
                    measurement_session_id=session.id,
                    family_member_id=profile.id,
                    recorded_at=moment,
                    metric_type="respiration_rate",
                    value=respiration_rate,
                    unit="breaths_per_min",
                )
            )
            db.add(
                VitalsReading(
                    measurement_session_id=session.id,
                    family_member_id=profile.id,
                    recorded_at=moment,
                    metric_type="hrv_rmssd",
                    value=hrv_rmssd,
                    unit="ms",
                )
            )

        db.commit()

        baselines = recompute_all_metrics(db, profile.id)
        db.commit()

        print(f"Profil '{PROFILE_NAME}' dibuat: {profile.id}")
        print(f"  {len(SOURCE_ROWS)} sesi pengukuran ditambahkan (14 hari mundur)")
        for baseline in baselines:
            print(
                f"  Baseline {baseline.metric_type}: "
                f"mean={float(baseline.mean_value):.1f}, "
                f"stddev={float(baseline.stddev_value):.1f}, "
                f"aktif={baseline.is_active}"
            )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Pemakaian: python scripts/seed_ml_sample_profile.py <email_akun>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
