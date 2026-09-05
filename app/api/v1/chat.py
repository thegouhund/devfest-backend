"""Endpoint chat (PRD FR-4.1, FR-4.3).

Frontend punya komponen chat sendiri, jadi di sini hanya ada endpoint JSON
biasa — bukan halaman chat utuh.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.chat.llm import ChatUnavailable
from app.core.security import get_current_account, get_current_profile
from app.db.models import Account, FamilyMember
from app.db.session import get_db
from app.schemas import (
    ChatConversationDetailResponse,
    ChatConversationListResponse,
    ChatConversationResponse,
    ChatMessageResponse,
    ChatRequest,
    ChatResponse,
)
from app.services import chat as chat_service
from app.services.conversation import (
    NotConversationOwner,
    get_history,
    list_conversations,
)


router = APIRouter()


@router.post("", response_model=ChatResponse)
def send_message(
    payload: ChatRequest,
    current_profile: FamilyMember = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> ChatResponse:
    """Kirim satu pesan, terima balasan.

    Butuh profil aktif, bukan hanya akun: agent menjawab atas nama profil
    yang sedang dipilih ("detak jantung saya" harus merujuk orang itu).
    Kosongkan `conversation_id` untuk memulai percakapan baru; isi dengan
    id sebelumnya supaya pertanyaan lanjutan tetap nyambung.
    """
    try:
        balasan, conversation = chat_service.ask(
            db,
            profile=current_profile,
            message=payload.message,
            conversation_id=payload.conversation_id,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except NotConversationOwner as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except ChatUnavailable as exc:
        # Chatbot adalah tambahan, bukan syarat: kegagalannya tidak boleh
        # terlihat seperti aplikasi rusak.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Layanan AI sedang tidak tersedia. Coba lagi sebentar lagi.",
        ) from exc

    db.commit()
    return ChatResponse(reply=balasan, conversation_id=conversation.id)


@router.get("/conversations", response_model=ChatConversationListResponse)
def read_conversations(
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ChatConversationListResponse:
    """Daftar sesi chat milik sendiri, terbaru lebih dulu."""
    rows = list_conversations(db, current_account.id)
    return ChatConversationListResponse(
        conversations=[ChatConversationResponse.model_validate(r) for r in rows],
        total=len(rows),
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ChatConversationDetailResponse,
)
def read_conversation(
    conversation_id: uuid.UUID,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ChatConversationDetailResponse:
    """Isi satu percakapan.

    Hanya pemiliknya — isi chat memuat cerita kesehatan pribadi dan tidak
    punya jalur berbagi seperti data vital.
    """
    try:
        pesan = get_history(db, conversation_id, viewer_account_id=current_account.id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except NotConversationOwner as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    return ChatConversationDetailResponse(
        id=conversation_id,
        messages=[ChatMessageResponse.model_validate(m) for m in pesan],
    )
