"""Lapisan sesi untuk UI chat Chainlit (PRD FR-4.5, A2).

Dipisah dari berkas aplikasi Chainlit supaya bisa diuji tanpa browser:
`chat_app.py` hanya merangkai fungsi-fungsi di sini ke event Chainlit.

Autentikasi memakai `security.py` yang sama dengan REST API, jadi user
tidak perlu login dua kali saat berpindah ke tab chat.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import resolve_current_user
from app.db.models import ConversationLog, User
from app.services.conversation import (
    append_message,
    end_conversation,
    start_conversation,
)


class ChatAuthError(PermissionError):
    """Sesi chat ditolak karena token tidak sah."""


def token_from_query(query: dict) -> str | None:
    """Ambil token dari query string iframe.

    Nilainya bisa berupa list (hasil `parse_qs`) atau string biasa,
    tergantung bagaimana Chainlit menyerahkan query-nya.
    """
    nilai = query.get("token")
    if not nilai:
        return None
    if isinstance(nilai, list):
        return nilai[0] if nilai else None
    return str(nilai)


def authenticate_token(db: Session, token: str | None) -> User:
    """Terjemahkan token jadi user aktif.

    Tanpa token yang sah, sesi ditolak — tidak ada mode anonim, karena
    seluruh isi chat menyentuh data kesehatan seseorang.
    """
    if not token:
        raise ChatAuthError("Token tidak ada. Buka chat dari aplikasi utama.")

    try:
        return resolve_current_user(db, token)
    except HTTPException as exc:
        # `resolve_current_user` menjawab 401 lewat HTTPException karena
        # dirancang untuk FastAPI; di sini diterjemahkan jadi error domain.
        raise ChatAuthError("Sesi tidak valid atau sudah kedaluwarsa") from exc


def open_chat_session(db: Session, user_id: uuid.UUID) -> ConversationLog:
    """Mulai percakapan baru. Pemanggil yang melakukan `commit`."""
    return start_conversation(db, user_id)


def record_turn(
    db: Session, conversation: ConversationLog, question: str, answer: str
) -> None:
    """Simpan satu giliran percakapan sebagai jejak audit.

    Jawaban kosong tetap membiarkan pertanyaannya tercatat: kalau model
    gagal menjawab, pertanyaan user tidak boleh ikut hilang dari audit.
    """
    if question and question.strip():
        append_message(db, conversation, "user", question)

    if answer and answer.strip():
        append_message(db, conversation, "assistant", answer)


def close_chat_session(
    db: Session, conversation: ConversationLog, summary: str | None = None
) -> None:
    """Tutup percakapan.

    Ringkasan opsional. Ekstraksi fakta kesehatan ke `health_facts`
    (Task 20) belum terpasang karena penyedia embedding belum dipilih.
    """
    end_conversation(db, conversation, summary=summary)
