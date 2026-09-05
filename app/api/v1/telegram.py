"""Endpoint penyambungan akun Telegram (PRD FR-5.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_current_account
from app.db.models import Account
from app.db.session import get_db
from app.schemas import TelegramLinkResponse, TelegramStatusResponse
from app.services import telegram as telegram_service


router = APIRouter()


@router.post("/link", response_model=TelegramLinkResponse)
def request_link_code(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> TelegramLinkResponse:
    """Terbitkan kode untuk dikirim user ke bot Telegram.

    Kode sekali pakai dan kedaluwarsa; meminta kode baru membatalkan yang
    lama.
    """
    link = telegram_service.issue_link_code(db, current_account.id)
    db.commit()
    db.refresh(link)

    return TelegramLinkResponse(
        link_code=link.link_code,
        bot_username=get_settings().telegram_bot_username or None,
        expires_at=link.link_code_expires_at,
    )


@router.get("/status", response_model=TelegramStatusResponse)
def read_link_status(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> TelegramStatusResponse:
    """Apakah akun sudah tersambung.

    Frontend melakukan polling ke sini setelah menampilkan kode, untuk tahu
    kapan user selesai mengirimkannya ke bot.
    """
    link = telegram_service.active_link(db, current_account.id)
    return TelegramStatusResponse(
        is_linked=link is not None,
        linked_at=link.linked_at if link else None,
    )


@router.delete("/link", status_code=status.HTTP_204_NO_CONTENT)
def remove_link(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> Response:
    telegram_service.unlink(db, current_account.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
