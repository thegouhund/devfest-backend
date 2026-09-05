"""Pengiriman pesan Telegram (PRD FR-5.1).

ponytail: fitur ini sengaja hanya MENGIRIM notifikasi, tidak menerima
pesan dari user — tidak ada webhook atau polling ke Telegram. Konsekuensinya,
tujuan pengiriman (`chat_id`) tidak bisa didapat lewat alur "user kirim kode
ke bot" seperti draf awal (ERD §2.13, kolom link_code di TelegramLink),
karena tidak ada yang mendengarkan pesan balik. Sebagai gantinya dipakai
satu TELEGRAM_DEFAULT_CHAT_ID tetap untuk semua akun — lihat
app/services/notification.py. Tabel TelegramLink dibiarkan ada di skema
(tidak ada migrasi drop) tapi tidak dipakai lagi.
"""

from __future__ import annotations

import httpx

from app.core.config import get_settings


class TelegramDeliveryError(RuntimeError):
    """Pesan gagal terkirim ke Telegram."""


TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10


def send_message(chat_id: str, text: str) -> None:
    """Kirim pesan lewat Bot API.

    Melempar `TelegramDeliveryError` untuk semua kegagalan, supaya pemanggil
    cukup menangani satu jenis error.
    """
    token = get_settings().telegram_bot_token
    if not token:
        raise TelegramDeliveryError("TELEGRAM_BOT_TOKEN belum di-set")

    try:
        response = httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=SEND_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise TelegramDeliveryError(f"Gagal mengirim ke Telegram: {exc}") from exc
