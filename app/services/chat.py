"""Menjalankan agent health companion untuk satu giliran percakapan.

Frontend sudah punya komponen chat sendiri, jadi backend hanya menyediakan
endpoint JSON — bukan halaman chat utuh. Riwayat percakapan diambil dari
database, bukan dikirim ulang frontend, supaya konteks tetap utuh walau
user memuat ulang halaman.
"""

from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.orm import Session

from app.chat.agent import build_agent
from app.chat.llm import ChatUnavailable
from app.db.models import ConversationLog, FamilyMember
from app.db.session import SessionLocal
from app.services.conversation import (
    NotConversationOwner,
    append_message,
    get_history,
    start_conversation,
)


# Batas giliran yang dikirim ulang ke model. Percakapan panjang akan
# melampaui jendela konteks dan menaikkan biaya tanpa menambah kualitas
# jawaban.
MAX_HISTORY_MESSAGES = 20

PESAN_GAGAL = (
    "Maaf, layanan AI sedang tidak bisa dihubungi. Coba lagi sebentar lagi."
)


def ask(
    db: Session,
    profile: FamilyMember,
    message: str,
    conversation_id: uuid.UUID | None = None,
) -> tuple[str, ConversationLog]:
    """Jalankan satu giliran percakapan.

    Percakapan melekat pada akun, tapi agent-nya bekerja atas nama profil
    aktif — "detak jantung saya" harus merujuk orang yang sedang dipilih,
    bukan seluruh keluarga.

    Mengembalikan `(balasan, percakapan)`. Melempar `ChatUnavailable` kalau
    penyedia AI tidak bisa dipakai, `LookupError` kalau percakapan tidak
    ditemukan, dan `NotConversationOwner` kalau bukan milik akun pemanggil.

    Pemanggil yang melakukan `commit`.
    """
    conversation = _resolve_conversation(db, profile, conversation_id)
    riwayat = _load_history(db, conversation, profile.account_id)

    # Agent memakai session factory sendiri: tool dijalankan LangGraph di
    # thread pool, dan satu Session tidak aman dipakai lintas thread.
    agent = build_agent(SessionLocal, profile)

    try:
        hasil = agent.invoke(
            {"messages": [*riwayat, HumanMessage(content=message)]}
        )
    except ChatUnavailable:
        raise
    except Exception as exc:
        # Detail teknis tidak dibocorkan ke user; pemanggil menerjemahkan
        # ini jadi 503 dengan pesan yang bisa dibaca orang.
        raise ChatUnavailable(PESAN_GAGAL) from exc

    balasan = _extract_reply(hasil)

    append_message(db, conversation, "user", message)
    if balasan.strip():
        append_message(db, conversation, "assistant", balasan)

    return balasan, conversation


def _resolve_conversation(
    db: Session, profile: FamilyMember, conversation_id: uuid.UUID | None
) -> ConversationLog:
    """Ambil percakapan yang diminta, atau mulai yang baru."""
    if conversation_id is None:
        return start_conversation(db, profile.account_id)

    conversation = db.get(ConversationLog, conversation_id)
    if conversation is None:
        raise LookupError("Percakapan tidak ditemukan")

    if conversation.account_id != profile.account_id:
        raise NotConversationOwner("Percakapan ini bukan milik Anda")

    return conversation


def _load_history(
    db: Session, conversation: ConversationLog, viewer_account_id: uuid.UUID
) -> list:
    """Giliran sebelumnya, dalam bentuk yang dimengerti agent.

    Diambil dari database, bukan dari kiriman frontend: kalau frontend yang
    menyusunnya, user bisa memalsukan "jawaban bot" untuk menggiring model.
    """
    try:
        pesan = get_history(db, conversation.id, viewer_account_id=viewer_account_id)
    except (LookupError, NotConversationOwner):
        return []

    terakhir = pesan[-MAX_HISTORY_MESSAGES:]
    return [
        HumanMessage(content=m.content)
        if m.role == "user"
        else AIMessage(content=m.content)
        for m in terakhir
        if m.role in ("user", "assistant")
    ]


def _extract_reply(hasil: dict) -> str:
    pesan = hasil.get("messages", [])
    terakhir = next((m for m in reversed(pesan) if isinstance(m, AIMessage)), None)
    return str(terakhir.content) if terakhir else PESAN_GAGAL
