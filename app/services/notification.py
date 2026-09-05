"""Pengiriman notifikasi anomali (PRD FR-5.2, FR-5.3).

Dua aturan yang dijaga di sini:

1. **Baris audit ditulis sebelum mencoba kirim.** Kalau ditulis setelahnya,
   kegagalan pengiriman berarti tidak ada jejak sama sekali — persis kasus
   yang paling perlu diselidiki.
2. **Kegagalan tidak pernah dilempar ke atas.** Fungsi ini dipanggil dari
   jalur deteksi anomali; Telegram yang sedang mati tidak boleh membatalkan
   anomali yang sudah benar terdeteksi dan tersimpan.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Anomaly, FamilyMember, Notification
from app.services.telegram import TelegramDeliveryError, active_link, send_message


CHANNEL_TELEGRAM = "telegram"

SEVERITY_LABEL = {
    "low": "Perlu diperhatikan",
    "medium": "Perlu diperhatikan",
    "high": "Penting",
}

METRIC_LABEL = {
    "heart_rate": "Detak jantung",
    "hrv_rmssd": "Variabilitas detak jantung",
    "respiration_rate": "Laju napas",
}


def notify_anomaly(db: Session, anomaly: Anomaly) -> list[Notification]:
    """Kirim notifikasi untuk satu anomali ke akun pemilik profil.

    Satu keluarga satu akun, dan Telegram tersambung di level akun — jadi
    penerimanya tunggal: akun yang menaungi profil subjek (FR-5.2). Nama
    subjek disebut dalam pesan supaya jelas ini soal siapa.

    Tidak pernah melempar exception. Pemanggil yang melakukan `commit`.
    """
    subject = db.get(FamilyMember, anomaly.family_member_id)
    if subject is None:
        return []

    message = build_message(db, anomaly, subject)
    return [_deliver(db, anomaly, subject.account_id, message)]


def _deliver(
    db: Session, anomaly: Anomaly, account_id: uuid.UUID, message: str
) -> Notification:
    """Tulis baris audit lalu coba kirim.

    Baris ditulis lebih dulu dan statusnya diperbarui setelah percobaan,
    jadi kegagalan tetap meninggalkan jejak.
    """
    notification = Notification(
        account_id=account_id,
        anomaly_id=anomaly.id,
        channel=CHANNEL_TELEGRAM,
        content=message,
        status="pending",
    )
    db.add(notification)
    db.flush()

    link = active_link(db, account_id)
    if link is None:
        # Belum menyambungkan Telegram: bukan kegagalan pengiriman, jadi
        # dibiarkan `pending` sebagai catatan bahwa pesannya tidak terkirim.
        return notification

    try:
        send_message(link.telegram_chat_id, message)
    except (TelegramDeliveryError, Exception):
        # Sengaja menangkap semua: kegagalan notifikasi tidak boleh
        # membatalkan anomali yang sudah tersimpan.
        notification.status = "failed"
        db.flush()
        return notification

    notification.status = "sent"
    notification.sent_at = datetime.now(UTC)
    db.flush()
    return notification


def build_message(db: Session, anomaly: Anomaly, subject: FamilyMember) -> str:
    """Susun isi pesan sesuai FR-5.3: metrik, nilai vs baseline, waktu,
    dan tautan ke detail."""
    settings = get_settings()
    metric = METRIC_LABEL.get(anomaly.metric_type, anomaly.metric_type)
    label = SEVERITY_LABEL.get(anomaly.severity, "Perlu diperhatikan")

    observed = float(anomaly.observed_value)
    mean = float(anomaly.baseline_mean)
    selisih = observed - mean
    arah = "di atas" if selisih > 0 else "di bawah"

    detected_at = anomaly.detected_at
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=UTC)
    waktu = detected_at.strftime("%d %b %Y, %H:%M UTC")

    # Nama subjek disebut karena satu admin bisa mengelola beberapa
    # dependent — tanpa ini dia tidak tahu ini soal siapa.
    tautan = f"{_frontend_url(settings)}/anomalies/{anomaly.id}"

    return (
        f"<b>{label}</b> — {subject.full_name}\n\n"
        f"{metric}: <b>{observed:.0f}</b> "
        f"({abs(selisih):.0f} {arah} rata-rata {mean:.0f})\n"
        f"Waktu: {waktu}\n\n"
        f"Lihat detail: {tautan}\n\n"
        "<i>Informasi ini bersifat wellness, bukan diagnosis medis.</i>"
    )


def _frontend_url(settings) -> str:
    """Alamat frontend untuk tautan dalam notifikasi.

    Diambil dari origin CORS pertama: itu alamat yang memang dilayani
    aplikasi ini, jadi tidak perlu setting terpisah yang bisa tidak sinkron.
    """
    origins = settings.cors_origins
    return origins[0] if origins else "http://localhost:5173"
