"""Ekstraksi vital sign dari video wajah (PRD FR-1.3).

Memakai `open-rppg` — model deep learning yang sudah menangani deteksi
wajah, ekstraksi sinyal, HRV, dan skor kualitas sekaligus. Pendekatan ini
divalidasi lebih dulu dengan rekaman nyata (HR 95.67 & 75.06 bpm pada
SQI 0.84 & 0.73) sebelum dipakai di sini.

Semua ambang di modul ini adalah konstanta bernama, bukan angka yang
tertanam di tengah algoritma: kamera, pencahayaan, dan skin tone di
lapangan tidak seideal video uji, jadi nilainya pasti perlu disetel ulang.

Catatan kompresi video: `open-rppg` memperingatkan bahwa frame non-key
merusak sinyal rPPG. Rekaman dari browser (MediaRecorder) hampir selalu
begitu, jadi skor kualitas rendah pada video hasil upload adalah hal yang
diperkirakan — bukan tanda ada bug. Flag kualitas ada justru untuk
menyaring kasus ini sebelum angkanya dianggap sahih.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Model dimuat malas: mengimpor JAX butuh waktu lama, dan proses yang tidak
# pernah memproses video tidak perlu menanggung biayanya.
_model = None


class RppgError(RuntimeError):
    """Ekstraksi gagal karena sebab teknis (file rusak, model error)."""


class SignalQualityError(RppgError):
    """Sinyal tidak layak dipakai — tidak ada wajah, atau hasilnya di luar
    nalar fisiologis. Beda dari kegagalan teknis: user perlu mengulang."""


# --- Tunable ---------------------------------------------------------------

# `open-rppg` mengembalikan breathing rate dalam Hz; ERD menyimpan
# breaths_per_min. Tanpa konversi, 0.22 Hz tersimpan apa adanya dan
# terbaca sebagai anomali ekstrem oleh detektor.
HZ_TO_BREATHS_PER_MIN = 60

# Batas nalar fisiologis. Lebih longgar dari rentang normal supaya kondisi
# ekstrem yang sah (bradikardia, olahraga berat) tetap tercatat — yang
# ditolak hanya angka yang jelas hasil sinyal kacau.
MIN_PLAUSIBLE_HR = 30
MAX_PLAUSIBLE_HR = 240

# Pemetaan SQI ke flag kualitas (ERD §2.4). Nilai awal dari pengamatan:
# rekaman bagus menghasilkan SQI 0.73-0.84.
# ponytail: ambang ini tebakan awal dari sedikit sampel; setel ulang setelah
# uji lapangan dengan variasi pencahayaan dan skin tone.
QUALITY_THRESHOLDS = {
    "good": 0.75,
    "fair": 0.50,
    "poor": 0.30,
}

# Kode metrik harus sama persis dengan seed `metric_types` (ERD §2.6),
# kalau tidak penyimpanan gagal karena foreign key.
METRIC_HEART_RATE = "heart_rate"
METRIC_HRV_RMSSD = "hrv_rmssd"
METRIC_RESPIRATION = "respiration_rate"

UNITS = {
    METRIC_HEART_RATE: "bpm",
    METRIC_HRV_RMSSD: "ms",
    METRIC_RESPIRATION: "breaths_per_min",
}


@dataclass(frozen=True)
class ExtractionResult:
    """Hasil satu sesi pengukuran."""

    heart_rate: float
    quality_score: float
    quality_flag: str
    hrv_rmssd: float | None = None
    respiration_rate: float | None = None

    def as_readings(self) -> list[dict]:
        """Bentuk siap simpan ke `vitals_readings` (long-format, ERD §2.7).

        Metrik yang tidak terukur dilewati, bukan disimpan sebagai nol —
        nol adalah nilai yang salah, ketiadaan data bukan.
        """
        values = {
            METRIC_HEART_RATE: self.heart_rate,
            METRIC_HRV_RMSSD: self.hrv_rmssd,
            METRIC_RESPIRATION: self.respiration_rate,
        }
        return [
            {"metric_type": metric, "value": value, "unit": UNITS[metric]}
            for metric, value in values.items()
            if value is not None
        ]


def hz_to_breaths_per_minute(hz: float | None) -> float | None:
    if hz is None:
        return None
    return hz * HZ_TO_BREATHS_PER_MIN


def classify_quality(score: float | None) -> str:
    """Petakan SQI ke flag `good|fair|poor|rejected` (ERD §2.4)."""
    if score is None:
        return "rejected"
    if score >= QUALITY_THRESHOLDS["good"]:
        return "good"
    if score >= QUALITY_THRESHOLDS["fair"]:
        return "fair"
    if score >= QUALITY_THRESHOLDS["poor"]:
        return "poor"
    return "rejected"


def _get_model():
    """Muat model sekali per proses.

    Pemanggilan pertama ~20 detik (JAX mengompilasi), berikutnya ~1 detik.
    Terukur: 19.6s lalu 1.0s untuk video 12 detik yang sama. Jadi biaya
    sebenarnya ada di kompilasi, bukan pemrosesan video — panggil
    `warm_up()` saat startup supaya pengukuran pertama user tidak
    menanggungnya.
    """
    global _model
    if _model is None:
        try:
            import rppg
        except ImportError as exc:
            # `exc` disertakan apa adanya: paket `open-rppg` sendiri bisa saja
            # terpasang tapi salah satu dependency-nya (jax, onnxruntime,
            # keras) yang gagal diimpor — pesan generik "belum terpasang"
            # menyesatkan penyelidikan kalau penyebabnya bukan itu.
            raise RppgError(
                f"Paket open-rppg atau salah satu dependency-nya gagal diimpor: {exc}. "
                "Kalau open-rppg sendiri sudah terpasang (cek: pip show open-rppg), "
                "masalahnya ada di salah satu dependency-nya."
            ) from exc
        _model = rppg.Model()
    return _model


def warm_up() -> None:
    """Muat model lebih awal supaya permintaan pertama tidak menunggu ~20 detik.

    Aman dipanggil berkali-kali; kegagalan sengaja tidak dilempar agar
    aplikasi tetap bisa start walau model bermasalah — kesalahannya akan
    muncul saat pengukuran, dengan pesan yang lebih jelas.
    """
    try:
        _get_model()
    except RppgError:
        pass


def _run_model(video_path: str) -> dict:
    """Jalankan model terhadap satu berkas video.

    Dipisah sebagai fungsi tersendiri supaya test bisa menggantinya tanpa
    memuat JAX.
    """
    return _get_model().process_video(video_path) or {}


def extract_vitals(video_path: str) -> ExtractionResult:
    """Ekstrak HR, HRV, dan respiration rate dari video wajah.

    Melempar `SignalQualityError` kalau wajah tidak terdeteksi atau hasilnya
    di luar nalar fisiologis; `RppgError` untuk kegagalan teknis.
    """
    if not Path(video_path).exists():
        raise RppgError(f"Berkas video tidak ditemukan: {video_path}")

    try:
        raw = _run_model(video_path)
    except Exception as exc:
        # Dibungkus jadi error domain supaya pemanggil tidak perlu tahu
        # detail internal model.
        raise RppgError(f"Pemrosesan rPPG gagal: {exc}") from exc

    heart_rate = raw.get("hr")
    if heart_rate is None:
        raise SignalQualityError(
            "Wajah tidak terdeteksi atau sinyal terlalu lemah. "
            "Pastikan wajah terlihat jelas dan pencahayaan cukup."
        )

    heart_rate = float(heart_rate)
    if not MIN_PLAUSIBLE_HR <= heart_rate <= MAX_PLAUSIBLE_HR:
        # Angka sampah lebih berbahaya daripada tidak ada angka: baseline
        # jadi kacau dan memicu alert palsu berhari-hari setelahnya.
        raise SignalQualityError(
            f"Hasil di luar rentang wajar ({heart_rate:.0f} bpm). "
            "Silakan ulangi pengukuran."
        )

    hrv = raw.get("hrv") or {}
    quality_score = float(raw.get("SQI") or 0.0)

    return ExtractionResult(
        heart_rate=heart_rate,
        quality_score=quality_score,
        quality_flag=classify_quality(quality_score),
        hrv_rmssd=_as_float(hrv.get("rmssd")),
        respiration_rate=hz_to_breaths_per_minute(_as_float(hrv.get("breathingrate"))),
    )


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN sering muncul saat sinyal terlalu pendek; perlakukan sebagai
    # tidak terukur, bukan sebagai nilai.
    return None if number != number else number
