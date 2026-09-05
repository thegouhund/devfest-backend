"""Penyimpanan percakapan chatbot (ERD §2.11, §2.12).

Ini jejak audit, bukan sumber memory chatbot. Fakta yang benar-benar
dipakai untuk mengingat riwayat kesehatan diekstrak ke `health_facts`
(Task 20) — pesan mentah di sini hanya untuk penelusuran.

Aturan akses berbeda dari data kesehatan lain: isi percakapan **tidak
pernah** terlihat orang lain. Vitals bisa dibagikan ke keluarga lewat
setelan privasi, tapi cerita yang diketik seseorang ke chatbot tidak punya
jalur berbagi sama sekali.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ConversationLog, ConversationMessage


class NotConversationOwner(PermissionError):
    """Pemanggil bukan pemilik percakapan ini."""


# Peran pesan yang dikenali (ERD §3).
VALID_ROLES = ("user", "assistant", "system", "tool")


def start_conversation(db: Session, account_id: uuid.UUID) -> ConversationLog:
    """Mulai sesi chat baru. Pemanggil yang melakukan `commit`."""
    conversation = ConversationLog(account_id=account_id, started_at=datetime.now(UTC))
    db.add(conversation)
    db.flush()
    return conversation


def append_message(
    db: Session, conversation: ConversationLog, role: str, content: str
) -> ConversationMessage:
    """Catat satu giliran percakapan.

    Urutan penyimpanan menentukan makna: kalau teracak, jawaban bot bisa
    terbaca sebagai pertanyaan user saat riwayat dimuat ulang.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Peran '{role}' tidak dikenal")

    if not content or not content.strip():
        raise ValueError("Isi pesan tidak boleh kosong")

    if conversation.ended_at is not None:
        # Sesi yang sudah ditutup punya ringkasan; menambah pesan membuat
        # ringkasannya tidak lagi cocok dengan isinya.
        raise ValueError("Percakapan sudah ditutup")

    message = ConversationMessage(
        conversation_id=conversation.id,
        sequence=_next_sequence(db, conversation.id),
        role=role,
        content=content,
    )
    db.add(message)
    db.flush()
    return message


def _next_sequence(db: Session, conversation_id: uuid.UUID) -> int:
    """Nomor urut giliran berikutnya dalam percakapan ini."""
    highest = db.execute(
        select(func.max(ConversationMessage.sequence)).where(
            ConversationMessage.conversation_id == conversation_id
        )
    ).scalar_one_or_none()
    return 0 if highest is None else highest + 1


def end_conversation(
    db: Session, conversation: ConversationLog, summary: str | None = None
) -> ConversationLog:
    """Tutup sesi dan simpan ringkasannya.

    Ringkasan opsional: pembuatannya melibatkan model yang bisa gagal, dan
    sesi tetap harus bisa ditutup tanpa itu.

    Menutup sesi yang sudah tertutup tidak mengubah waktu penutupannya —
    waktu pertama yang benar.
    """
    if conversation.ended_at is None:
        conversation.ended_at = datetime.now(UTC)

    if summary is not None:
        conversation.summary = summary

    db.flush()
    return conversation


def get_history(
    db: Session, conversation_id: uuid.UUID, viewer_account_id: uuid.UUID
) -> list[ConversationMessage]:
    """Seluruh pesan dalam satu percakapan, terurut.

    Melempar `LookupError` kalau percakapan tidak ada, atau
    `NotConversationOwner` kalau bukan milik akun pemanggil. Percakapan
    melekat pada akun, bukan profil: chat dibuka dari sesi login, dan
    tidak ada jalur berbagi isi chat antar akun.
    """
    conversation = db.get(ConversationLog, conversation_id)
    if conversation is None:
        raise LookupError("Percakapan tidak ditemukan")

    if conversation.account_id != viewer_account_id:
        raise NotConversationOwner("Percakapan ini bukan milik Anda")

    return (
        db.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence)
        )
        .scalars()
        .all()
    )


def list_conversations(
    db: Session, account_id: uuid.UUID, limit: int = 50
) -> list[ConversationLog]:
    """Daftar sesi chat milik user, terbaru lebih dulu."""
    return (
        db.execute(
            select(ConversationLog)
            .where(ConversationLog.account_id == account_id)
            .order_by(ConversationLog.started_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
