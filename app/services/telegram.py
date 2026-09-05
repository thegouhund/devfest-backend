"""Penyambungan akun Telegram dan pengiriman pesan (PRD FR-5.1).

Alur linking (FR-5.1): user menekan tombol di web, sistem menerbitkan kode
unik, user mengirim kode itu ke bot, bot menukarkannya jadi sambungan.

Kode dibuat sekali pakai dan kedaluwarsa: kode yang bocor lewat screenshot
atau salah kirim tidak boleh berlaku selamanya, karena siapa pun yang
menukarkannya akan menerima notifikasi kesehatan keluarga ini.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import TelegramLink


class TelegramDeliveryError(RuntimeError):
    """Pesan gagal terkirim ke Telegram."""


# Alfabet tanpa karakter yang mudah tertukar saat dibacakan atau disalin,
# sama seperti kode undangan family.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

# Cukup lama untuk berpindah dari browser ke aplikasi Telegram, cukup
# singkat supaya kode yang bocor tidak berguna lama.
LINK_CODE_TTL = timedelta(minutes=15)

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10


def generate_link_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def issue_link_code(db: Session, account_id: uuid.UUID) -> TelegramLink:
    """Terbitkan kode linking baru untuk seorang user.

    Menimpa kode sebelumnya kalau ada, supaya tidak ada dua kode aktif
    sekaligus. Pemanggil yang melakukan `commit`.
    """
    link = db.execute(
        select(TelegramLink).where(TelegramLink.account_id == account_id)
    ).scalar_one_or_none()

    if link is None:
        link = TelegramLink(account_id=account_id)
        db.add(link)

    link.link_code = generate_link_code()
    link.link_code_expires_at = datetime.now(UTC) + LINK_CODE_TTL
    db.flush()
    return link


def consume_link_code(db: Session, code: str, chat_id: str) -> TelegramLink:
    """Tukarkan kode jadi sambungan aktif.

    Melempar `LookupError` kalau kode tidak dikenal, sudah dipakai, atau
    kedaluwarsa — ketiganya dijawab sama supaya tidak membocorkan kode mana
    yang pernah ada.

    Pemanggil yang melakukan `commit`.
    """
    link = db.execute(
        select(TelegramLink).where(TelegramLink.link_code == code)
    ).scalar_one_or_none()

    if link is None:
        raise LookupError("Kode tidak valid atau sudah kedaluwarsa")

    expires_at = link.link_code_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if expires_at is None or expires_at < datetime.now(UTC):
        raise LookupError("Kode tidak valid atau sudah kedaluwarsa")

    link.telegram_chat_id = chat_id
    # Kode dihapus setelah dipakai: sekali pakai berarti tidak bisa
    # menyambungkan akun Telegram kedua.
    link.link_code = None
    link.link_code_expires_at = None
    link.is_active = True
    link.linked_at = datetime.now(UTC)
    db.flush()
    return link


def active_link(db: Session, account_id: uuid.UUID) -> TelegramLink | None:
    """Sambungan aktif milik user, atau None kalau belum tersambung.

    Kode yang sudah diterbitkan tapi belum ditukarkan tidak dihitung
    tersambung — `telegram_chat_id`-nya masih kosong.
    """
    link = db.execute(
        select(TelegramLink).where(
            TelegramLink.account_id == account_id,
            TelegramLink.telegram_chat_id.isnot(None),
            TelegramLink.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return link


def unlink(db: Session, account_id: uuid.UUID) -> None:
    """Putuskan sambungan. Pemanggil yang melakukan `commit`."""
    link = db.execute(
        select(TelegramLink).where(TelegramLink.account_id == account_id)
    ).scalar_one_or_none()
    if link is not None:
        db.delete(link)
        db.flush()


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
