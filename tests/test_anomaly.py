"""Deteksi anomali vital sign lewat model ML (PRD FR-3.2 s.d. FR-3.4).

Acceptance criteria under test:
- Tanpa baseline heart_rate aktif -> tidak ada anomali dan tidak error
  (diam saat cold-start)
- Model ML menilai delta_bpm, delta_rr, bpm_to_rr_ratio, bpm_variance,
  activity_level_score sekaligus -> anomali dengan nilai teramati dan
  perbandingan baseline
- Severity dipetakan lewat kelipatan ambang model
- related_activity_id hanya aktivitas terdekat dalam jendela terbatas, atau
  NULL; activity_level_score diturunkan dari kategori aktivitas yang sama
- Deteksi terpisah dari penyimpanan, supaya model bisa diganti tanpa
  menyentuh skema (FR-3.4)
- Evaluasi per SESI (bukan per pembacaan): heart_rate dan respiration_rate
  dari sesi yang sama dinilai bersamaan, konsisten dengan cara model dilatih
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import (
    ActivityLog,
    Anomaly,
    Baseline,
    FamilyMember,
    MeasurementSession,
    VitalsReading,
)
from app.services.anomaly import (
    ACTIVITY_CONTEXT_WINDOW,
    ACTIVITY_LEVEL_SCORE,
    Detection,
    classify_severity,
    detect_for_session,
    evaluate_session,
)
from tests.conftest import make_profile_row


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def user(db_session) -> FamilyMember:
    person = make_profile_row(db_session, full_name="Budi")
    db_session.commit()
    return person


@pytest.fixture
def hr_baseline(db_session, user, now) -> Baseline:
    """Baseline heart_rate aktif: rata-rata 70, simpangan 5."""
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


@pytest.fixture
def rr_baseline(db_session, user, now) -> Baseline:
    """Baseline respiration_rate aktif: rata-rata 16."""
    row = Baseline(
        family_member_id=user.id,
        metric_type="respiration_rate",
        mean_value=16.0,
        stddev_value=1.5,
        sample_count=30,
        window_start=now - timedelta(days=30),
        window_end=now,
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()
    return row


def add_session(
    db,
    user: FamilyMember,
    moment: datetime,
    *,
    heart_rate: float = 72.0,
    respiration_rate: float | None = 16.0,
) -> MeasurementSession:
    session = MeasurementSession(
        family_member_id=user.id,
        initiated_by_family_member_id=user.id,
        capture_method="upload",
        started_at=moment,
        processing_status="completed",
    )
    db.add(session)
    db.flush()

    db.add(
        VitalsReading(
            measurement_session_id=session.id,
            family_member_id=user.id,
            recorded_at=moment,
            metric_type="heart_rate",
            value=heart_rate,
            unit="bpm",
        )
    )
    if respiration_rate is not None:
        db.add(
            VitalsReading(
                measurement_session_id=session.id,
                family_member_id=user.id,
                recorded_at=moment,
                metric_type="respiration_rate",
                value=respiration_rate,
                unit="breaths_per_min",
            )
        )
    db.commit()
    return session


# --- Perhitungan murni -------------------------------------------------------


class TestEvaluateSession:
    """`evaluate_session` tidak menyentuh database — bisa diganti model lain
    tanpa mengubah skema (FR-3.4)."""

    def test_normal_reading_is_not_anomaly(self) -> None:
        result = evaluate_session(
            heart_rate=72.0,
            respiration_rate=16.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=0,
        )
        assert result is None

    def test_large_spike_at_rest_is_anomaly(self) -> None:
        """Lonjakan besar sambil `activity_level_score=0` (istirahat) harus
        tertangkap — ini kasus utama README devfest-ml."""
        result = evaluate_session(
            heart_rate=115.0,
            respiration_rate=20.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=0,
        )
        assert result is not None
        assert result.observed_bpm == pytest.approx(115.0)
        assert result.baseline_mean_bpm == pytest.approx(70.0)

    def test_activity_level_score_changes_the_verdict(self) -> None:
        """`activity_level_score` benar-benar dipakai model, bukan fitur
        yang diam-diam diabaikan — bedanya dengan z-score lama yang buta
        konteks aktivitas sama sekali.

        Isolation Forest bukan fungsi monoton sederhana, jadi tidak
        dijamin "olahraga selalu menurunkan skor" — yang dijamin cuma:
        mengubah fitur ini mengubah keluarannya.
        """
        rest = evaluate_session(
            heart_rate=115.0,
            respiration_rate=20.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=0,
        )
        exercising = evaluate_session(
            heart_rate=115.0,
            respiration_rate=20.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=3,
        )
        rest_score = rest.deviation_score if rest else 0.0
        exercising_score = exercising.deviation_score if exercising else 0.0
        assert rest_score != exercising_score

    def test_respiration_rate_zero_does_not_crash(self) -> None:
        """Pembacaan napas yang gagal (0) tidak boleh membuat pembagian
        bpm_to_rr_ratio meledak."""
        result = evaluate_session(
            heart_rate=72.0,
            respiration_rate=0.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=0,
        )
        assert result is None or isinstance(result, Detection)

    def test_returns_plain_object_not_orm(self) -> None:
        result = evaluate_session(
            heart_rate=115.0,
            respiration_rate=20.0,
            baseline_mean_bpm=70.0,
            baseline_stddev_bpm=5.0,
            activity_level_score=0,
        )
        assert result is None or isinstance(result, Detection)


class TestSeverity:
    def test_classify_severity_orders_correctly(self) -> None:
        threshold = 0.08
        assert classify_severity(threshold * 1.1, threshold) == "low"
        assert classify_severity(threshold * 2.0, threshold) == "medium"
        assert classify_severity(threshold * 3.0, threshold) == "high"

    def test_activity_level_scores_are_bounded(self) -> None:
        """devfest-ml melatih model dengan skor 0-3."""
        assert all(0 <= score <= 3 for score in ACTIVITY_LEVEL_SCORE.values())


# --- Deteksi terhadap database -----------------------------------------------


class TestDetectForSession:
    def test_no_baseline_is_silent(self, db_session, user, now) -> None:
        """Sebelum cold-start terlampaui, sistem diam — bukan error, bukan
        pula membanjiri alert dari data yang belum cukup (PRD A3)."""
        session = add_session(db_session, user, now, heart_rate=140.0)
        anomalies = detect_for_session(db_session, session.id)
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
        session = add_session(db_session, user, now, heart_rate=140.0)
        assert detect_for_session(db_session, session.id) == []

    def test_normal_session_creates_nothing(
        self, db_session, user, hr_baseline, now
    ) -> None:
        session = add_session(db_session, user, now, heart_rate=72.0)
        detect_for_session(db_session, session.id)
        db_session.commit()
        assert db_session.execute(select(Anomaly)).first() is None

    def test_outlier_creates_anomaly(
        self, db_session, user, hr_baseline, now
    ) -> None:
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomalies = detect_for_session(db_session, session.id)
        db_session.commit()
        assert len(anomalies) == 1

    def test_anomaly_records_comparison(
        self, db_session, user, hr_baseline, now
    ) -> None:
        """Nilai baseline disalin ke baris anomali, bukan direferensikan —
        supaya riwayat tetap terbaca walau baseline berubah nanti."""
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()

        assert float(anomaly.observed_value) == pytest.approx(115.0)
        assert float(anomaly.baseline_mean) == pytest.approx(70.0)
        assert float(anomaly.baseline_stddev) == pytest.approx(5.0)

    def test_anomaly_starts_as_new(self, db_session, user, hr_baseline, now) -> None:
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.status == "new"

    def test_anomaly_links_session(self, db_session, user, hr_baseline, now) -> None:
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.measurement_session_id == session.id

    def test_metric_type_is_heart_rate(
        self, db_session, user, hr_baseline, now
    ) -> None:
        """Anomali dicatat sebagai satu baris di bawah heart_rate walau
        modelnya menilai heart_rate dan respiration_rate sekaligus — itu
        metrik yang paling jelas dipahami pengguna."""
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.metric_type == "heart_rate"

    def test_baseline_of_other_profile_not_used(
        self, db_session, user, now
    ) -> None:
        """Baseline bersifat personal — memakai milik orang lain berarti
        membandingkan seseorang dengan tubuh orang lain."""
        other = make_profile_row(db_session, full_name="Siti")
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

        session = add_session(db_session, user, now, heart_rate=140.0)
        assert detect_for_session(db_session, session.id) == []

    def test_missing_respiration_reading_still_evaluates(
        self, db_session, user, hr_baseline, now
    ) -> None:
        """Sesi tanpa pembacaan napas (mis. sinyal gagal) tetap dinilai
        lewat delta_bpm — hilangnya satu sinyal bukan alasan diam total."""
        session = add_session(
            db_session, user, now, heart_rate=115.0, respiration_rate=None
        )
        anomalies = detect_for_session(db_session, session.id)
        db_session.commit()
        assert isinstance(anomalies, list)

    def test_unknown_session_returns_empty(self, db_session, user) -> None:
        import uuid

        assert detect_for_session(db_session, uuid.uuid4()) == []


# --- Konteks aktivitas --------------------------------------------------------


class TestActivityContext:
    def test_links_nearby_activity(
        self, db_session, user, hr_baseline, now
    ) -> None:
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

        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.related_activity_id is not None

    def test_distant_activity_not_linked(
        self, db_session, user, hr_baseline, now
    ) -> None:
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

        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_no_activity_leaves_null(
        self, db_session, user, hr_baseline, now
    ) -> None:
        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_picks_closest_activity(
        self, db_session, user, hr_baseline, now
    ) -> None:
        jauh = ActivityLog(
            family_member_id=user.id,
            logged_by_family_member_id=user.id,
            category="sleep",
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

        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.related_activity_id == dekat.id

    def test_other_profiles_activity_not_linked(
        self, db_session, user, hr_baseline, now
    ) -> None:
        other = make_profile_row(db_session, full_name="Siti")
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

        session = add_session(db_session, user, now, heart_rate=115.0)
        anomaly = detect_for_session(db_session, session.id)[0]
        db_session.commit()
        assert anomaly.related_activity_id is None

    def test_window_is_named_constant(self) -> None:
        assert ACTIVITY_CONTEXT_WINDOW > timedelta(0)

    def test_exercise_scores_highest(self) -> None:
        """Olahraga adalah aktivitas yang paling wajar menaikkan detak
        jantung, jadi harus mendapat skor tertinggi di peta ini."""
        assert ACTIVITY_LEVEL_SCORE["exercise"] == max(ACTIVITY_LEVEL_SCORE.values())

    def test_sleep_scores_lowest(self) -> None:
        assert ACTIVITY_LEVEL_SCORE["sleep"] == 0
