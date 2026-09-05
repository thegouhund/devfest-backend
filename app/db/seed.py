"""Seed data referensi.

`metric_types` adalah tabel lookup (ERD §2.6), bukan enum — menambah metrik
baru cukup menambah entri di sini atau INSERT langsung, tanpa migrasi skema.
"""

from sqlalchemy.orm import Session

from app.db.models import MetricType


SEED_METRIC_TYPES = [
    {
        "code": "heart_rate",
        "display_name": "Heart Rate",
        "default_unit": "bpm",
        "category": "vital",
    },
    {
        "code": "hrv_rmssd",
        "display_name": "Heart Rate Variability (RMSSD)",
        "default_unit": "ms",
        "category": "vital",
    },
    {
        "code": "respiration_rate",
        "display_name": "Respiration Rate",
        "default_unit": "breaths_per_min",
        "category": "vital",
    },
]


def seed_metric_types(db: Session) -> int:
    """Insert metrik yang belum ada. Aman dijalankan berulang.

    Baris yang sudah ada sengaja tidak di-update supaya perubahan lokal
    (mis. menonaktifkan metrik lewat `is_active`) tidak ter-reset.
    """
    inserted = 0
    for metric in SEED_METRIC_TYPES:
        if db.get(MetricType, metric["code"]) is None:
            db.add(MetricType(**metric))
            inserted += 1
    db.flush()
    return inserted
