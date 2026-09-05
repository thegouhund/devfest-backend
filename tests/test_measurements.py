"""Task 10: endpoint sesi pengukuran.

Acceptance criteria under test:
- Upload balik 202 dengan session id seketika; pemrosesan tidak inline
- Status pending -> processing -> completed; exception jadi failed dengan
  alasan, tidak pernah menggantung di processing
- Sukses menulis satu baris vitals_readings per metrik, user_id
  didenormalisasi dari sesi
- Admin boleh mengukur dependent yang dikelolanya; mengukur orang lain 403
- Hasil membawa flag kualitas dan disclaimer FR-1.6
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import MeasurementSession, User, VideoStorageRef, VitalsReading
from app.services import rppg as rppg_service
from app.services.rppg import ExtractionResult, RppgError, SignalQualityError


MEASUREMENTS = "/api/v1/measurements"
FAMILIES = "/api/v1/families"


def fake_mp4(size: int = 4096) -> bytes:
    return b"\x00\x00\x00\x20ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * size


def upload(client, headers, *, content=None, filename="rekaman.mp4", data=None):
    return client.post(
        f"{MEASUREMENTS}/upload",
        files={"file": (filename, content or fake_mp4(), "video/mp4")},
        data=data or {},
        headers=headers,
    )


@pytest.fixture
def storage(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("VIDEO_STORAGE_PATH", str(tmp_path / "videos"))
    monkeypatch.setenv("JWT_SECRET", "secret-khusus-test-yang-cukup-panjang-32b")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def hasil_bagus(monkeypatch):
    """Stub ekstraksi berhasil — nilai dari rekaman nyata."""
    hasil = ExtractionResult(
        heart_rate=72.4,
        quality_score=0.87,
        quality_flag="good",
        hrv_rmssd=45.2,
        respiration_rate=16.1,
    )
    monkeypatch.setattr(rppg_service, "extract_vitals", lambda path: hasil)
    import app.services.measurement as measurement_service

    monkeypatch.setattr(measurement_service, "extract_vitals", lambda path: hasil)
    return hasil


@pytest.fixture
def dependent(client, auth_headers):
    """Dependent yang dikelola `registered_user`."""
    family = client.post(
        FAMILIES, json={"name": "Keluarga"}, headers=auth_headers
    ).json()
    response = client.post(
        f"{FAMILIES}/{family['id']}/dependents",
        json={"full_name": "Anak"},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def orang_lain(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "lain@example.com",
            "password": "rahasia-kuat-123",
            "full_name": "Orang Lain",
        },
    )
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    return {"headers": headers, "id": user_id}


# --- Upload ----------------------------------------------------------------


class TestUpload:
    def test_returns_202_with_session_id(
        self, client, auth_headers, storage, hasil_bagus
    ) -> None:
        response = upload(client, auth_headers)
        assert response.status_code == 202, response.text
        body = response.json()
        assert uuid.UUID(body["session_id"])
        assert body["processing_status"] in ("pending", "processing", "completed")

    def test_creates_session_row(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.capture_method == "upload"

    def test_stores_video_reference(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        ref = db_session.execute(select(VideoStorageRef)).scalar_one()
        assert ref.storage_provider == "vps_local"

    def test_rejects_non_video(self, client, auth_headers, storage) -> None:
        response = upload(client, auth_headers, filename="skrip.sh")
        assert response.status_code == 422

    def test_rejects_fake_video_content(self, client, auth_headers, storage) -> None:
        """Ekstensi .mp4 dengan isi bukan video harus ditolak."""
        response = upload(client, auth_headers, content=b"#!/bin/sh\n" + b"\x00" * 4096)
        assert response.status_code == 422

    def test_requires_authentication(self, client, storage) -> None:
        response = client.post(
            f"{MEASUREMENTS}/upload",
            files={"file": ("a.mp4", fake_mp4(), "video/mp4")},
        )
        assert response.status_code == 401


# --- Mengukur atas nama orang lain -----------------------------------------


class TestMeasureOnBehalf:
    def test_admin_can_measure_own_dependent(
        self, client, auth_headers, storage, hasil_bagus, dependent, db_session
    ) -> None:
        response = upload(client, auth_headers, data={"user_id": dependent["id"]})
        assert response.status_code == 202, response.text

        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert str(session.user_id) == dependent["id"]
        assert str(session.initiated_by_user_id) != dependent["id"]

    def test_cannot_measure_unrelated_user(
        self, client, auth_headers, storage, hasil_bagus, orang_lain
    ) -> None:
        """Mengukur orang yang bukan dependent-nya = menulis data kesehatan
        atas nama orang lain."""
        response = upload(client, auth_headers, data={"user_id": orang_lain["id"]})
        assert response.status_code == 403

    def test_cannot_measure_nonexistent_user(
        self, client, auth_headers, storage, hasil_bagus
    ) -> None:
        response = upload(client, auth_headers, data={"user_id": str(uuid.uuid4())})
        assert response.status_code in (403, 404)

    def test_self_measurement_sets_both_ids_same(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.user_id == session.initiated_by_user_id


# --- Pemrosesan ------------------------------------------------------------


class TestProcessing:
    def test_success_writes_one_reading_per_metric(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        readings = db_session.execute(select(VitalsReading)).scalars().all()
        assert {r.metric_type for r in readings} == {
            "heart_rate",
            "hrv_rmssd",
            "respiration_rate",
        }

    def test_reading_user_id_denormalised_from_session(
        self, client, auth_headers, storage, hasil_bagus, dependent, db_session
    ) -> None:
        """user_id di vitals_readings harus subjek, bukan yang mengukur —
        kalau tertukar, data anak masuk ke grafik orang tuanya."""
        upload(client, auth_headers, data={"user_id": dependent["id"]})
        reading = db_session.execute(select(VitalsReading)).scalars().first()
        assert str(reading.user_id) == dependent["id"]

    def test_status_becomes_completed(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.processing_status == "completed"

    def test_quality_recorded_on_session(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        upload(client, auth_headers)
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert float(session.signal_quality_score) == pytest.approx(0.87)
        assert session.signal_quality_flag == "good"

    def test_baseline_recomputed_after_measurement(
        self, client, auth_headers, storage, hasil_bagus, db_session
    ) -> None:
        """Baseline harus ikut diperbarui, kalau tidak deteksi anomali
        selalu membandingkan terhadap angka basi."""
        from app.db.models import Baseline

        upload(client, auth_headers)
        baselines = db_session.execute(select(Baseline)).scalars().all()
        assert {b.metric_type for b in baselines} == {
            "heart_rate",
            "hrv_rmssd",
            "respiration_rate",
        }

    def test_baseline_failure_does_not_undo_measurement(
        self, client, auth_headers, storage, hasil_bagus, monkeypatch, db_session
    ) -> None:
        """Pengukuran yang sudah tersimpan tidak boleh hilang hanya karena
        perhitungan baseline gagal."""
        import app.services.measurement as measurement_service

        def gagal(db, user_id):
            raise RuntimeError("hitung baseline gagal")

        monkeypatch.setattr(measurement_service, "recompute_all_metrics", gagal)

        upload(client, auth_headers)
        db_session.expire_all()
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.processing_status == "completed"
        assert db_session.execute(select(VitalsReading)).first() is not None

    def test_anomaly_detected_against_existing_baseline(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """Pengukuran yang jauh menyimpang harus memunculkan anomali,
        memakai baseline yang sudah aktif."""
        from datetime import UTC, datetime, timedelta

        from app.db.models import Anomaly, Baseline
        import app.services.measurement as measurement_service

        me = db_session.execute(select(User)).scalars().first()
        now = datetime.now(UTC)
        db_session.add(
            Baseline(
                user_id=me.id,
                metric_type="heart_rate",
                mean_value=70.0,
                stddev_value=5.0,
                sample_count=30,
                window_start=now - timedelta(days=30),
                window_end=now + timedelta(days=1),
                is_active=True,
            )
        )
        db_session.commit()

        melonjak = ExtractionResult(
            heart_rate=130.0, quality_score=0.9, quality_flag="good"
        )
        monkeypatch.setattr(
            measurement_service, "extract_vitals", lambda path: melonjak
        )

        upload(client, auth_headers)
        db_session.expire_all()
        anomalies = db_session.execute(select(Anomaly)).scalars().all()
        assert len(anomalies) == 1
        assert anomalies[0].severity == "high"

    def test_no_anomaly_during_cold_start(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """User baru tanpa baseline aktif tidak boleh dapat alert."""
        from app.db.models import Anomaly
        import app.services.measurement as measurement_service

        melonjak = ExtractionResult(
            heart_rate=130.0, quality_score=0.9, quality_flag="good"
        )
        monkeypatch.setattr(
            measurement_service, "extract_vitals", lambda path: melonjak
        )

        upload(client, auth_headers)
        assert db_session.execute(select(Anomaly)).first() is None

    def test_partial_result_stores_available_metrics(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """Sinyal lemah kadang hanya menghasilkan HR — itu tetap disimpan."""
        import app.services.measurement as measurement_service

        hasil = ExtractionResult(
            heart_rate=68.0, quality_score=0.55, quality_flag="fair"
        )
        monkeypatch.setattr(measurement_service, "extract_vitals", lambda path: hasil)

        upload(client, auth_headers)
        readings = db_session.execute(select(VitalsReading)).scalars().all()
        assert len(readings) == 1
        assert readings[0].metric_type == "heart_rate"


# --- Jalur kegagalan -------------------------------------------------------


class TestFailurePaths:
    def test_extraction_error_marks_failed_not_stuck(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """Sesi tidak boleh menggantung di `processing` selamanya — frontend
        akan polling tanpa akhir."""
        import app.services.measurement as measurement_service

        def gagal(path):
            raise RppgError("model meledak")

        monkeypatch.setattr(measurement_service, "extract_vitals", gagal)

        upload(client, auth_headers)
        db_session.expire_all()
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.processing_status == "failed"

    def test_quality_rejection_marks_failed(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        import app.services.measurement as measurement_service

        def ditolak(path):
            raise SignalQualityError("wajah tidak terdeteksi")

        monkeypatch.setattr(measurement_service, "extract_vitals", ditolak)

        upload(client, auth_headers)
        db_session.expire_all()
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.processing_status == "failed"
        assert session.signal_quality_flag == "rejected"

    def test_failure_writes_no_readings(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """Ekstraksi gagal tidak boleh meninggalkan angka separuh jadi."""
        import app.services.measurement as measurement_service

        monkeypatch.setattr(
            measurement_service,
            "extract_vitals",
            lambda path: (_ for _ in ()).throw(RppgError("gagal")),
        )
        upload(client, auth_headers)
        assert db_session.execute(select(VitalsReading)).first() is None

    def test_unexpected_error_also_marks_failed(
        self, client, auth_headers, storage, monkeypatch, db_session
    ) -> None:
        """Exception apa pun, bukan hanya RppgError, harus tetap menutup sesi."""
        import app.services.measurement as measurement_service

        monkeypatch.setattr(
            measurement_service,
            "extract_vitals",
            lambda path: (_ for _ in ()).throw(ZeroDivisionError("tak terduga")),
        )
        upload(client, auth_headers)
        db_session.expire_all()
        session = db_session.execute(select(MeasurementSession)).scalar_one()
        assert session.processing_status == "failed"


# --- Status & hasil --------------------------------------------------------


class TestReadSession:
    def test_status_endpoint(
        self, client, auth_headers, storage, hasil_bagus
    ) -> None:
        session_id = upload(client, auth_headers).json()["session_id"]
        response = client.get(f"{MEASUREMENTS}/{session_id}", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["processing_status"] == "completed"

    def test_results_contain_readings_and_disclaimer(
        self, client, auth_headers, storage, hasil_bagus
    ) -> None:
        session_id = upload(client, auth_headers).json()["session_id"]
        body = client.get(
            f"{MEASUREMENTS}/{session_id}/results", headers=auth_headers
        ).json()

        assert body["signal_quality_flag"] == "good"
        assert body["disclaimer"], "disclaimer FR-1.6 wajib ada"
        metrics = {r["metric_type"]: r for r in body["readings"]}
        assert metrics["heart_rate"]["unit"] == "bpm"
        assert metrics["respiration_rate"]["unit"] == "breaths_per_min"

    def test_results_before_completion_returns_409(
        self, client, auth_headers, storage, monkeypatch
    ) -> None:
        """Meminta hasil sesi yang gagal harus jelas, bukan array kosong
        yang terbaca seolah pengukurannya normal."""
        import app.services.measurement as measurement_service

        monkeypatch.setattr(
            measurement_service,
            "extract_vitals",
            lambda path: (_ for _ in ()).throw(RppgError("gagal")),
        )
        session_id = upload(client, auth_headers).json()["session_id"]
        response = client.get(
            f"{MEASUREMENTS}/{session_id}/results", headers=auth_headers
        )
        assert response.status_code == 409

    def test_other_user_cannot_read_session(
        self, client, auth_headers, storage, hasil_bagus, orang_lain
    ) -> None:
        session_id = upload(client, auth_headers).json()["session_id"]
        response = client.get(
            f"{MEASUREMENTS}/{session_id}", headers=orang_lain["headers"]
        )
        assert response.status_code in (403, 404)

    def test_other_user_cannot_read_results(
        self, client, auth_headers, storage, hasil_bagus, orang_lain
    ) -> None:
        session_id = upload(client, auth_headers).json()["session_id"]
        response = client.get(
            f"{MEASUREMENTS}/{session_id}/results", headers=orang_lain["headers"]
        )
        assert response.status_code in (403, 404)

    def test_unknown_session_returns_404(self, client, auth_headers, storage) -> None:
        response = client.get(f"{MEASUREMENTS}/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404


class TestListSessions:
    def test_lists_own_sessions(
        self, client, auth_headers, storage, hasil_bagus
    ) -> None:
        upload(client, auth_headers)
        upload(client, auth_headers)
        body = client.get(MEASUREMENTS, headers=auth_headers).json()
        assert body["total"] == 2
        assert len(body["sessions"]) == 2

    def test_does_not_list_other_users_sessions(
        self, client, auth_headers, storage, hasil_bagus, orang_lain
    ) -> None:
        upload(client, auth_headers)
        body = client.get(MEASUREMENTS, headers=orang_lain["headers"]).json()
        assert body["total"] == 0

    def test_inaccessible_user_forbidden(
        self, client, auth_headers, storage, hasil_bagus, orang_lain
    ) -> None:
        """403 utuh, bukan daftar kosong — konsisten dengan endpoint vitals,
        activities, dan anomalies. Daftar kosong terbaca seolah orangnya
        belum pernah mengukur."""
        upload(client, auth_headers)
        me = client.get("/api/v1/users/me", headers=auth_headers).json()["id"]
        response = client.get(
            f"{MEASUREMENTS}?user_id={me}", headers=orang_lain["headers"]
        )
        assert response.status_code == 403

    def test_pagination(self, client, auth_headers, storage, hasil_bagus) -> None:
        for _ in range(3):
            upload(client, auth_headers)
        body = client.get(f"{MEASUREMENTS}?limit=2", headers=auth_headers).json()
        assert len(body["sessions"]) == 2
        assert body["total"] == 3
