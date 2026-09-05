"""Task 12: perhitungan baseline personal (PRD FR-3.1).

Acceptance criteria under test:
- Data < 14 hari -> baris ditulis dengan is_active=false
- Mencapai ambang -> is_active=true dengan mean/stddev/sample_count benar
- Upsert menghormati UNIQUE(user_id, metric_type, window_end)
- Window dengan satu sampel tidak menghasilkan stddev nol/NaN yang membuat
  setiap z-score berikutnya tak hingga
- Hari cold-start dan panjang window dari settings, bukan angka tertanam
"""

from __future__ import annotations

import statistics as py_statistics
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Baseline, MeasurementSession, User, VitalsReading
from app.services.baseline import (
    MIN_SAMPLES_FOR_BASELINE,
    MIN_STDDEV,
    compute_baseline,
    recompute_for_user,
)


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def user(db_session) -> User:
    person = User(full_name="Budi", email="budi@example.com")
    db_session.add(person)
    db_session.commit()
    return person


def add_readings(
    db,
    user: User,
    values: list[tuple[datetime, float]],
    metric: str = "heart_rate",
) -> None:
    session = MeasurementSession(
        user_id=user.id,
        initiated_by_user_id=user.id,
        capture_method="upload",
        started_at=values[0][0],
        processing_status="completed",
    )
    db.add(session)
    db.flush()
    for moment, value in values:
        db.add(
            VitalsReading(
                measurement_session_id=session.id,
                user_id=user.id,
                recorded_at=moment,
                metric_type=metric,
                value=value,
                unit="bpm",
            )
        )
    db.commit()


def daily(now: datetime, values: list[float]) -> list[tuple[datetime, float]]:
    """Satu pembacaan per hari, mundur dari hari ini."""
    return [(now - timedelta(days=i), v) for i, v in enumerate(values)]


# --- Cold start ------------------------------------------------------------


class TestColdStart:
    def test_insufficient_history_is_inactive(self, db_session, user, now) -> None:
        """Kurang dari 14 hari data: baseline tetap dihitung tapi belum
        dipakai untuk alert (PRD A3)."""
        add_readings(db_session, user, daily(now, [70.0] * 5))
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert baseline is not None
        assert baseline.is_active is False

    def test_sufficient_history_activates(self, db_session, user, now) -> None:
        add_readings(db_session, user, daily(now, [70 + i % 5 for i in range(20)]))
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert baseline.is_active is True

    def test_threshold_comes_from_settings(
        self, db_session, user, now, monkeypatch
    ) -> None:
        """Ambang cold-start harus bisa diubah tanpa menyentuh kode —
        PRD §13 masih membuka kemungkinan penyetelan ulang."""
        from app.core.config import get_settings

        monkeypatch.setenv("BASELINE_COLD_START_DAYS", "3")
        monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
        get_settings.cache_clear()

        add_readings(db_session, user, daily(now, [70.0, 72.0, 74.0, 71.0, 73.0]))
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert baseline.is_active is True
        get_settings.cache_clear()

    def test_no_readings_returns_none(self, db_session, user) -> None:
        assert recompute_for_user(db_session, user.id, "heart_rate") is None


# --- Ketepatan hitungan ----------------------------------------------------


class TestStatisticalCorrectness:
    def test_mean_and_stddev_match_stdlib(self, db_session, user, now) -> None:
        """Diperiksa terhadap `statistics` bawaan Python, bukan angka yang
        saya hitung sendiri.

        Sebarannya sengaja lebih lebar dari MIN_STDDEV supaya yang diuji
        ketepatan hitungan, bukan lantai yang menimpanya.
        """
        values = [58.0, 72.0, 85.0, 64.0, 79.0, 61.0, 76.0]
        assert py_statistics.stdev(values) > MIN_STDDEV, "data uji harus di atas lantai"
        add_readings(db_session, user, daily(now, values))

        result = compute_baseline(values)
        assert result.mean == pytest.approx(py_statistics.mean(values))
        assert result.stddev == pytest.approx(py_statistics.stdev(values))
        assert result.sample_count == len(values)

    def test_floor_only_applies_below_threshold(self) -> None:
        """Sebaran yang sudah lebar tidak boleh ikut diubah lantai."""
        wide = [55.0, 90.0, 65.0, 80.0, 70.0]
        assert compute_baseline(wide).stddev == pytest.approx(
            py_statistics.stdev(wide)
        )

    def test_persisted_values_match_computation(self, db_session, user, now) -> None:
        values = [60.0, 84.0, 72.0, 58.0, 86.0]
        add_readings(db_session, user, daily(now, values))

        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.mean_value) == pytest.approx(py_statistics.mean(values))
        assert float(baseline.stddev_value) == pytest.approx(
            py_statistics.stdev(values)
        )

    def test_window_bounds_recorded(self, db_session, user, now) -> None:
        add_readings(db_session, user, daily(now, [70.0] * 5))
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert baseline.window_start < baseline.window_end


# --- Varians degenerate ----------------------------------------------------


class TestDegenerateVariance:
    def test_single_sample_has_usable_stddev(self, db_session, user, now) -> None:
        """Satu sampel: stdev tidak terdefinisi. Kalau dibiarkan nol,
        setiap pembacaan berikutnya jadi z-score tak hingga dan semuanya
        ditandai anomali."""
        add_readings(db_session, user, [(now, 72.0)])
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.stddev_value) >= MIN_STDDEV

    def test_identical_values_have_usable_stddev(
        self, db_session, user, now
    ) -> None:
        """Nilai yang persis sama menghasilkan stdev nol — masalah yang sama."""
        add_readings(db_session, user, daily(now, [72.0] * 10))
        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.stddev_value) >= MIN_STDDEV

    def test_stddev_floor_is_named(self) -> None:
        assert MIN_STDDEV > 0

    def test_zero_stddev_would_make_every_reading_anomalous(self) -> None:
        """Menunjukkan kenapa lantai stddev perlu ada."""
        result = compute_baseline([72.0, 72.0, 72.0])
        z_score = abs(75.0 - result.mean) / result.stddev
        assert z_score != float("inf")

    def test_empty_values_returns_none(self) -> None:
        assert compute_baseline([]) is None

    def test_floor_tolerates_instrument_error(self) -> None:
        """Lantai stddev harus mencerminkan galat alat, bukan sekadar
        bukan-nol. rPPG sendiri meleset beberapa bpm (uji Task 9: target 72,
        terbaca 73.35), jadi selisih sekecil itu tidak boleh jadi alert."""
        baseline = compute_baseline([72.0] * 5)
        z_small = abs(75.0 - baseline.mean) / baseline.stddev
        assert z_small < 2.0, "selisih dalam galat alat tidak boleh jadi anomali"

    def test_floor_still_catches_real_spike(self) -> None:
        """Lonjakan nyata tetap harus terdeteksi walau baseline seragam."""
        baseline = compute_baseline([72.0] * 5)
        z_large = abs(95.0 - baseline.mean) / baseline.stddev
        assert z_large > 2.0


# --- Upsert ----------------------------------------------------------------


class TestUpsert:
    def test_recompute_replaces_same_window(self, db_session, user, now) -> None:
        """UNIQUE(user_id, metric_type, window_end): hitung ulang di window
        yang sama harus menimpa, bukan menambah baris."""
        add_readings(db_session, user, daily(now, [70.0] * 5))
        recompute_for_user(db_session, user.id, "heart_rate", window_end=now)
        db_session.commit()
        recompute_for_user(db_session, user.id, "heart_rate", window_end=now)
        db_session.commit()

        rows = (
            db_session.execute(
                select(Baseline).where(Baseline.user_id == user.id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    def test_different_metrics_are_separate(self, db_session, user, now) -> None:
        add_readings(db_session, user, daily(now, [70.0] * 5))
        add_readings(
            db_session, user, daily(now, [16.0] * 5), metric="respiration_rate"
        )
        recompute_for_user(db_session, user.id, "heart_rate", window_end=now)
        recompute_for_user(db_session, user.id, "respiration_rate", window_end=now)
        db_session.commit()

        rows = (
            db_session.execute(select(Baseline).where(Baseline.user_id == user.id))
            .scalars()
            .all()
        )
        assert {r.metric_type for r in rows} == {"heart_rate", "respiration_rate"}

    def test_recompute_updates_values(self, db_session, user, now) -> None:
        add_readings(db_session, user, [(now, 70.0)])
        recompute_for_user(db_session, user.id, "heart_rate", window_end=now)
        db_session.commit()

        add_readings(db_session, user, [(now - timedelta(hours=1), 90.0)])
        baseline = recompute_for_user(db_session, user.id, "heart_rate", window_end=now)
        db_session.commit()
        assert float(baseline.mean_value) == pytest.approx(80.0)


# --- Isolasi antar user ----------------------------------------------------


class TestUserIsolation:
    def test_baseline_only_uses_own_readings(self, db_session, user, now) -> None:
        """Baseline personal: data orang lain tidak boleh ikut terhitung."""
        other = User(full_name="Siti", email="siti@example.com")
        db_session.add(other)
        db_session.commit()

        add_readings(db_session, user, daily(now, [70.0] * 5))
        add_readings(db_session, other, daily(now, [120.0] * 5))

        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.mean_value) == pytest.approx(70.0)

    def test_metric_isolation(self, db_session, user, now) -> None:
        add_readings(db_session, user, daily(now, [70.0] * 5))
        add_readings(db_session, user, daily(now, [16.0] * 5), metric="respiration_rate")

        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.mean_value) == pytest.approx(70.0)


# --- Batas window ----------------------------------------------------------


class TestWindow:
    def test_old_readings_excluded(self, db_session, user, now) -> None:
        """Baseline harus mengikuti kondisi terkini; data setahun lalu
        bukan cerminan kondisi sekarang."""
        add_readings(db_session, user, [(now - timedelta(days=400), 120.0)])
        add_readings(db_session, user, daily(now, [70.0] * 5))

        baseline = recompute_for_user(db_session, user.id, "heart_rate")
        db_session.commit()
        assert float(baseline.mean_value) == pytest.approx(70.0)

    def test_min_samples_is_named(self) -> None:
        assert MIN_SAMPLES_FOR_BASELINE >= 1
