"""Status konfigurasi Telegram (PRD FR-5.1).

ponytail: notifikasi dikirim ke satu TELEGRAM_DEFAULT_CHAT_ID untuk semua
akun (lihat app/services/notification.py), bukan lewat linking per-akun —
alur linking asli butuh webhook yang menerima pesan dari bot, yang di luar
cakupan saat ini. Endpoint di sini cuma melapor status konfigurasi global,
tidak ada lagi kode atau chat_id per-akun.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.security import get_current_account
from app.db.models import Account
from app.schemas import TelegramStatusResponse


router = APIRouter()


@router.get("/status", response_model=TelegramStatusResponse)
def read_telegram_status(
    current_account: Account = Depends(get_current_account),
) -> TelegramStatusResponse:
    """Apakah notifikasi Telegram sudah dikonfigurasi di server ini.

    Sama untuk semua akun — bukan status linking per-akun, karena tidak
    ada lagi proses linking.
    """
    settings = get_settings()
    return TelegramStatusResponse(
        is_configured=bool(settings.telegram_bot_token)
        and bool(settings.telegram_default_chat_id)
    )
