"""Task 13: deteksi anomali statistik (PRD FR-3.2, FR-3.3).

Acceptance criteria under test:
- Tanpa baseline aktif -> tidak ada anomali dan tidak error (diam saat cold-start)
- |z| melewati ambang -> anomali dengan nilai teramati, mean/stddev baseline,
  dan skor deviasi
- Severity dipetakan lewat konstanta bernama
- related_activity_id hanya aktivitas terdekat dalam jendela terbatas, atau NULL
- Deteksi terpisah dari penyimpanan, supaya model bisa diganti tanpa
  menyentuh skema (FR-3.4)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    ActivityLog,
    Anomaly,
    Baseline,
    MeasurementSession,
    FamilyMember,
    VitalsReading,
)
from app.services.anomaly import (
    ACTIVITY_CONTEXT_WINDOW,
    SEVERITY_THRESHOLDS,
    Detection,
    classify_severity,
    detect,
    evaluate_reading,
)
from tests.conftest import make_account, make_profile_row


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def user(db_session) -> FamilyMember:
    person = make_profile_row(db_session, full_name="Budi")
    db_session.commit()
    return person


@pytest.fixture
def baseline(db_session, user, now) -> Baseline:
    """Baseline aktif: rata-rata 70, simpangan 5."""
    row = Baseline(
        family_member_id=user.id,
        metric_type="heart_rate",
        mean_value=70.0,
        stddev_value=5.0,
        sample_count=30,
        window_start=now - timedelta(days=30),
        window_end=now,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def add_reading(db, user: FamilyMember, value: float, moment: datetime) -> VitalsReading:
    session = MeasurementSession(
        family_member_id=user.id,
        initiated_by_family_member_id=user.id,
        capture_method="upload",
        started_at=moment,
        processing_status="completed",
    )
    db.add(session)
    db.flush()
    reading = VitalsReading(
        measurement_session_id=session.id,
        family_member_id=user.id,
        recorded_at=moment,
        metric_type="heart_rate",
        value=value,
        unit="bpm",
    )
    db.add(reading)
    db.commit()
    return reading


# --- Perhitungan murni -----------------------------------------------------


class TestEvaluateReading:
    """`evaluate_reading` tidak menyentuh database — bisa diganti model lain
    tanpa mengubah skema (FR-3.4)."""

    def test_within_threshold_is_not_anomaly(self) -> None:
        result = evaluate_reading(observed=72.0, mean=70.0, stddev=5.0, threshold=2.0)
        assert result is None

    def test_beyond_threshold_is_anomaly(self) -> None:
        result = evaluate_reading(observed=95.0, mean=70.0, stddev=5.0, threshold=2.0)
        assert result is not None
        assert result.deviation_score == pytest.approx(5.0)

    def test_detects_low_side_too(self) -> None:
        """Turun jauh sama pentingnya dengan naik jauh — bradikardia juga
        kondisi yang perlu diperhatikan."""
        result = evaluate_reading(observed=45.0, mean=70.0, stddev=5.0, threshold=2.0)
        assert result is not None
        assert result.deviation_score == pytest.approx(5.0)

    def test_exactly_at_threshold_is_not_anomaly(self) -> None:
        """Batas harus tegas: tepat di ambang belum dianggap menyimpang."""
        result = evaluate_reading(observed=80.0, mean=70.0, stddev=5.0, threshold=2.0)
        assert result is None

    def test_zero_stddev_does_not_divide_by_zero(self) -> None:
        """Baseline sudah punya lantai stddev, tapi fungsi ini harus tetap
        aman kalau dipanggil dengan nol dari sumber lain."""
        result = evaluate_reading(observed=95.0, mean=70.0, stddev=0.0, threshold=2.0)
        assert result is None or result.deviation_score != float("inf")

    def test_returns_plain_object_not_orm(self) -> None:
        result = evaluate_reading(observed=95.0, mean=70.0, stddev=5.0, threshold=2.0)
        assert isinstance(result, Detection)


class TestSeverity:
    @pytest.mark.parametrize(
        ("z", "expected"),
        [(2.1, "low"), (2.9, "low"), (3.0, "medium"), (3.9, "medium"), (4.0, "high"), (8.0, "high")],
    )
    def test_maps_deviation_to_severity(self, z: float, expected: str) -> None:
        assert classify_severity(z) == expected

    def test_thresholds_are_named(self) -> None:
        assert set(SEVERITY_THRESHOLDS) == {"medium", "high"}
        assert SEVERITY_THRESHOLDS["high"] > SEVERITY_THRESHOLDS["medium"]


# --- Deteksi terhadap database ---------------------------------------------


class TestDetect:
    def test_no_baseline_is_silent(self, db_session, user, now) -> None:
        """Sebelum cold-start terlampaui, sistem diam — bukan error, bukan
        pula membanjiri alert dari data yang belum cukup (PRD A3)."""
        reading = add_reading(db_session, user, 120.0, now)
        anomalies = detect(db_session, reading)
        db_session.commit()
        assert anomalies == []

    def test_inactive_baseline_is_silent(self, db_session, user, now) -> None:
        db_session.add(
            Baseline(
                family_member_id=user.id,
                metric_type="heart_rate",
                mean_value=70.0,
                stddev_value=5.0,
                sample_count=3,
                window_start=now - timedelta(days=3),
                window_end=now,
                is_active=False,
            )
        )
        db_session.commit()
        reading = add_reading(db_session, user, 120.0, now)
        assert detect(db_session, reading) == []

    def test_normal_reading_creates_nothing(
        self, db_session, user, baseline, now
    ) -> None:
        reading = add_reading(db_session, user, 72.0, now)
        detect(db_session, reading)
        db_session.commit()
        assert db_session.execute(select(Anomaly)).first() is None

    def test_outlier_creates_anomaly(self, db_session, user, baseline, now) -> None:
        reading = add_reading(db_session, user, 95.0, now)
        anomalies = detect(db_session, reading)
        db_session.commit()
        assert len(anomalies) == 1

    def test_anomaly_records_comparison(self, db_session, user, baseline, now) -> None:
        """Nilai baseline disalin ke baris anomali, bukan direferensikan —
        supaya riwayat tetap terbaca walau baseline berubah nanti."""
        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()

        assert float(anomaly.observed_value) == pytest.approx(95.0)
        assert float(anomaly.baseline_mean) == pytest.approx(70.0)
        assert float(anomaly.baseline_stddev) == pytest.approx(5.0)
        assert float(anomaly.deviation_score) == pytest.approx(5.0)

    def test_anomaly_starts_as_new(self, db_session, user, baseline, now) -> None:
        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.status == "new"

    def test_anomaly_links_session(self, db_session, user, baseline, now) -> None:
        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.measurement_session_id == reading.measurement_session_id

    def test_severity_reflects_deviation(self, db_session, user, baseline, now) -> None:
        reading = add_reading(db_session, user, 130.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.severity == "high"

    def test_baseline_of_other_user_not_used(self, db_session, user, now) -> None:
        """Baseline bersifat personal — memakai milik orang lain berarti
        membandingkan seseorang dengan tubuh orang lain."""
        other = make_profile_row(db_session, full_name="Siti")
        db_session.add(other)
        db_session.flush()
        db_session.add(
            Baseline(
                family_member_id=other.id,
                metric_type="heart_rate",
                mean_value=70.0,
                stddev_value=5.0,
                sample_count=30,
                window_start=now - timedelta(days=30),
                window_end=now,
                is_active=True,
            )
        )
        db_session.commit()

        reading = add_reading(db_session, user, 130.0, now)
        assert detect(db_session, reading) == []

    def test_threshold_comes_from_settings(
        self, db_session, user, baseline, now, monkeypatch
    ) -> None:
        """PRD §13 masih membuka penyetelan ambang per metrik."""
        from app.core.config import get_settings

        monkeypatch.setenv("ANOMALY_ZSCORE_THRESHOLD", "10.0")
        monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
        get_settings.cache_clear()

        reading = add_reading(db_session, user, 95.0, now)
        assert detect(db_session, reading) == []
        get_settings.cache_clear()


# --- Konteks aktivitas -----------------------------------------------------


class TestActivityContext:
    def test_links_nearby_activity(self, db_session, user, baseline, now) -> None:
        """Aktivitas terdekat waktu dilampirkan supaya chatbot bisa
        menjelaskan kemungkinan penyebab (FR-3.3)."""
        db_session.add(
            ActivityLog(
                family_member_id=user.id,
                logged_by_family_member_id=user.id,
                category="coffee",
                quantity=3,
                unit="cups",
                source="menu",
                occurred_at=now - timedelta(minutes=30),
            )
        )
        db_session.commit()

        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.related_activity_id is not None

    def test_distant_activity_not_linked(self, db_session, user, baseline, now) -> None:
        """Tanpa batas jendela, kopi tiga hari lalu akan dikaitkan sebagai
        penyebab — penjelasan yang menyesatkan."""
        db_session.add(
            ActivityLog(
                family_member_id=user.id,
                logged_by_family_member_id=user.id,
                category="coffee",
                source="menu",
                occurred_at=now - ACTIVITY_CONTEXT_WINDOW - timedelta(hours=1),
            )
        )
        db_session.commit()

        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_no_activity_leaves_null(self, db_session, user, baseline, now) -> None:
        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_picks_closest_activity(self, db_session, user, baseline, now) -> None:
        jauh = ActivityLog(
            family_member_id=user.id,
            logged_by_family_member_id=user.id,
            category="exercise",
            source="menu",
            occurred_at=now - timedelta(hours=3),
        )
        dekat = ActivityLog(
            family_member_id=user.id,
            logged_by_family_member_id=user.id,
            category="coffee",
            source="menu",
            occurred_at=now - timedelta(minutes=10),
        )
        db_session.add_all([jauh, dekat])
        db_session.commit()

        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.related_activity_id == dekat.id

    def test_other_users_activity_not_linked(
        self, db_session, user, baseline, now
    ) -> None:
        other = make_profile_row(db_session, full_name="Siti")
        db_session.add(other)
        db_session.flush()
        db_session.add(
            ActivityLog(
                family_member_id=other.id,
                logged_by_family_member_id=other.id,
                category="coffee",
                source="menu",
                occurred_at=now - timedelta(minutes=10),
            )
        )
        db_session.commit()

        reading = add_reading(db_session, user, 95.0, now)
        anomaly = detect(db_session, reading)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_window_is_named_constant(self) -> None:
        assert ACTIVITY_CONTEXT_WINDOW > timedelta(0)
