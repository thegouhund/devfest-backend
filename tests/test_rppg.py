"""Task 9: ekstraksi sinyal rPPG dari video wajah.

Mengandalkan `open-rppg` (model deep learning), bukan POS manual —
pendekatan ini sudah divalidasi dengan rekaman nyata di repo
`testing-lomba-devfest` (HR 95.67 & 75.06 bpm, SQI 0.84 & 0.73).

Acceptance criteria under test:
- Mengembalikan HR, HRV, respiration rate, plus skor & flag kualitas
- Video tanpa wajah -> ditolak, bukan menghasilkan angka palsu
- Semua tunable berupa konstanta bernama, bukan angka tertanam di algoritma
- Satuan respiration rate breaths_per_min (library mengembalikan Hz)
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services import rppg as rppg_service
from app.services.rppg import (
    HZ_TO_BREATHS_PER_MIN,
    MAX_PLAUSIBLE_HR,
    MIN_PLAUSIBLE_HR,
    QUALITY_THRESHOLDS,
    RppgError,
    SignalQualityError,
    ExtractionResult,
    classify_quality,
    extract_vitals,
    hz_to_breaths_per_minute,
)


@pytest.fixture
def video_path(tmp_path) -> str:
    """Berkas video tiruan. Isinya tidak dibaca karena `_run_model`
    di-stub; yang penting berkasnya ada, sebab `extract_vitals`
    memeriksa keberadaan file lebih dulu."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 1024)
    return str(path)


# --- Konversi satuan -------------------------------------------------------


class TestUnitConversion:
    def test_hz_to_breaths_per_minute(self) -> None:
        """Library mengembalikan Hz; ERD menyimpan breaths_per_min.

        Tanpa konversi, 0.22 Hz tersimpan apa adanya dan detektor anomali
        akan membacanya sebagai napas 0.22/menit — anomali ekstrem palsu.
        """
        assert hz_to_breaths_per_minute(0.2267) == pytest.approx(13.6, abs=0.1)

    def test_conversion_factor_is_named(self) -> None:
        assert HZ_TO_BREATHS_PER_MIN == 60

    def test_none_stays_none(self) -> None:
        assert hz_to_breaths_per_minute(None) is None

    def test_typical_resting_rate_lands_in_normal_range(self) -> None:
        """0.2-0.33 Hz adalah rentang istirahat normal -> 12-20 napas/menit."""
        for hz in (0.2, 0.25, 0.33):
            bpm = hz_to_breaths_per_minute(hz)
            assert 11 <= bpm <= 21, f"{hz} Hz -> {bpm}"


# --- Klasifikasi kualitas --------------------------------------------------


class TestQualityClassification:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.95, "good"),
            (0.85, "good"),
            (0.70, "fair"),
            (0.55, "fair"),
            (0.40, "poor"),
            (0.20, "rejected"),
            (0.0, "rejected"),
        ],
    )
    def test_maps_score_to_flag(self, score: float, expected: str) -> None:
        assert classify_quality(score) == expected

    def test_thresholds_are_named_constants(self) -> None:
        """Ambang kualitas perlu disetel ulang setelah uji lapangan —
        kamera dan pencahayaan nyata tidak seideal video uji."""
        assert set(QUALITY_THRESHOLDS) == {"good", "fair", "poor"}
        assert QUALITY_THRESHOLDS["good"] > QUALITY_THRESHOLDS["fair"]
        assert QUALITY_THRESHOLDS["fair"] > QUALITY_THRESHOLDS["poor"]

    def test_none_score_is_rejected(self) -> None:
        assert classify_quality(None) == "rejected"

    def test_boundary_is_inclusive(self) -> None:
        assert classify_quality(QUALITY_THRESHOLDS["good"]) == "good"


# --- Validasi hasil --------------------------------------------------------


class TestPlausibilityGuard:
    def test_bounds_are_named(self) -> None:
        assert MIN_PLAUSIBLE_HR < 50
        assert MAX_PLAUSIBLE_HR > 180

    @pytest.mark.parametrize("hr", [0, 5, 300, 1000])
    def test_implausible_hr_rejected(self, hr: float, monkeypatch, video_path) -> None:
        """Angka di luar nalar fisiologis lebih baik ditolak daripada
        disimpan — data sampah akan meracuni baseline dan memicu alert palsu."""
        monkeypatch.setattr(
            rppg_service, "_run_model", lambda path: {"hr": hr, "SQI": 0.9, "hrv": {}}
        )
        with pytest.raises(SignalQualityError):
            extract_vitals(video_path)

    @pytest.mark.parametrize("hr", [45, 60, 100, 180])
    def test_plausible_hr_accepted(self, hr: float, monkeypatch, video_path) -> None:
        monkeypatch.setattr(
            rppg_service,
            "_run_model",
            lambda path: {"hr": hr, "SQI": 0.9, "hrv": {"rmssd": 40}},
        )
        assert extract_vitals(video_path).heart_rate == hr


# --- Ekstraksi -------------------------------------------------------------


class TestExtractVitals:
    @pytest.fixture
    def hasil_model(self):
        """Bentuk keluaran nyata open-rppg, disalin dari CSV hasil uji
        di repo testing-lomba-devfest."""
        return {
            "hr": 95.67,
            "SQI": 0.8418506,
            "hrv": {
                "bpm": 95.14640086084839,
                "ibi": 630.6071428571429,
                "sdnn": 37.364575954788876,
                "rmssd": 46.27902184843452,
                "breathingrate": 0.22669311419665628,
            },
        }

    def test_returns_all_three_metrics(self, monkeypatch, hasil_model, video_path) -> None:
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: hasil_model)
        result = extract_vitals(video_path)
        assert result.heart_rate == pytest.approx(95.67)
        assert result.hrv_rmssd == pytest.approx(46.279, abs=0.01)
        assert result.respiration_rate == pytest.approx(13.6, abs=0.1)

    def test_respiration_converted_to_breaths_per_minute(
        self, monkeypatch, hasil_model, video_path
    ) -> None:
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: hasil_model)
        result = extract_vitals(video_path)
        # 0.2267 Hz mentah akan terbaca sebagai anomali ekstrem.
        assert result.respiration_rate > 1

    def test_quality_score_and_flag(self, monkeypatch, hasil_model, video_path) -> None:
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: hasil_model)
        result = extract_vitals(video_path)
        assert result.quality_score == pytest.approx(0.8419, abs=0.001)
        assert result.quality_flag == "good"

    def test_as_readings_matches_erd_metric_codes(self, monkeypatch, hasil_model, video_path) -> None:
        """Kode metrik harus sama persis dengan seed `metric_types`,
        kalau tidak penyimpanan gagal karena foreign key."""
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: hasil_model)
        readings = extract_vitals(video_path).as_readings()
        assert {r["metric_type"] for r in readings} == {
            "heart_rate",
            "hrv_rmssd",
            "respiration_rate",
        }

    def test_as_readings_carries_units(self, monkeypatch, hasil_model, video_path) -> None:
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: hasil_model)
        units = {
            r["metric_type"]: r["unit"]
            for r in extract_vitals(video_path).as_readings()
        }
        assert units == {
            "heart_rate": "bpm",
            "hrv_rmssd": "ms",
            "respiration_rate": "breaths_per_min",
        }

    def test_missing_hrv_does_not_crash(self, monkeypatch, video_path) -> None:
        """Sinyal lemah kadang menghasilkan HR tanpa HRV — itu hasil parsial
        yang sah, bukan alasan menggagalkan seluruh pengukuran."""
        monkeypatch.setattr(
            rppg_service, "_run_model", lambda path: {"hr": 72, "SQI": 0.8, "hrv": {}}
        )
        result = extract_vitals(video_path)
        assert result.heart_rate == 72
        assert result.hrv_rmssd is None
        assert len(result.as_readings()) == 1

    def test_no_face_is_rejected(self, monkeypatch, video_path) -> None:
        """Video tanpa wajah harus ditolak, bukan menghasilkan angka palsu."""
        monkeypatch.setattr(rppg_service, "_run_model", lambda path: {})
        with pytest.raises(SignalQualityError):
            extract_vitals(video_path)

    def test_model_failure_becomes_domain_error(self, monkeypatch, video_path) -> None:
        """Kegagalan library dibungkus jadi error domain, supaya pemanggil
        tidak perlu tahu detail internal model."""

        def meledak(path):
            raise RuntimeError("model gagal dimuat")

        monkeypatch.setattr(rppg_service, "_run_model", meledak)
        with pytest.raises(RppgError):
            extract_vitals(video_path)

    def test_missing_file_raises_domain_error(self) -> None:
        with pytest.raises(RppgError):
            extract_vitals("/tmp/tidak-ada-file-ini-12345.mp4")


# --- Integrasi dengan model asli -------------------------------------------


@pytest.mark.slow
class TestRealModel:
    """Menjalankan model sungguhan. Lambat (memuat JAX), jadi ditandai
    `slow` dan dilewati kecuali diminta: `pytest -m slow`."""

    def test_synthetic_pulse_video(self, tmp_path) -> None:
        """Video sintetis berdenyut pada frekuensi diketahui.

        Ini bukan wajah manusia, jadi model wajar menolaknya — yang diuji
        di sini adalah tidak adanya crash dan penolakan yang bersih.
        """
        cv2 = pytest.importorskip("cv2")

        fps, duration, bpm = 30, 10, 72
        path = tmp_path / "pulse.mp4"
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (320, 240)
        )
        for i in range(fps * duration):
            phase = np.sin(2 * np.pi * (bpm / 60) * (i / fps))
            frame = np.full((240, 320, 3), 128, dtype=np.uint8)
            frame[:, :, 2] = np.clip(150 + 12 * phase, 0, 255)
            writer.write(frame)
        writer.release()

        try:
            result = extract_vitals(str(path))
            assert MIN_PLAUSIBLE_HR <= result.heart_rate <= MAX_PLAUSIBLE_HR
        except SignalQualityError:
            pass  # tidak ada wajah: penolakan yang benar
