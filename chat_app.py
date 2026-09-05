"""Aplikasi Chainlit untuk health companion (PRD FR-4.5, A2).

Berjalan sebagai proses terpisah dari API, di-embed frontend sebagai iframe:

    <iframe src="http://localhost:8001?token=<access_token>" />

Jalankan dengan:

    chainlit run chat_app.py --port 8001

Token yang dipakai sama dengan REST API, jadi user tidak perlu login dua
kali. Berkas ini sengaja tipis — seluruh logika ada di `app/chat/session.py`
supaya bisa diuji tanpa browser.
"""

from __future__ import annotations

import chainlit as cl
from langchain_core.messages import AIMessage, HumanMessage

from app.chat.agent import build_agent
from app.chat.llm import ChatUnavailable
from app.chat.session import (
    ChatAuthError,
    authenticate_token,
    close_chat_session,
    open_chat_session,
    record_turn,
)
from app.db.session import SessionLocal


SAPAAN = (
    "Halo {nama}. Saya bisa bantu menjelaskan data kesehatanmu, mencatat "
    "aktivitas harian, atau membahas hasil pengukuran terakhir.\n\n"
    "_Informasi di sini bersifat wellness, bukan diagnosis medis._"
)

PESAN_GAGAL = (
    "Maaf, terjadi gangguan saat memproses pertanyaanmu. Coba lagi sebentar lagi."
)


@cl.on_chat_start
async def mulai() -> None:
    """Autentikasi lewat token di query string, lalu siapkan agent."""
    token = _token_dari_permintaan()

    db = SessionLocal()
    try:
        try:
            user = authenticate_token(db, token)
        except ChatAuthError as exc:
            await cl.Message(content=f"{exc}").send()
            return

        try:
            agent = build_agent(SessionLocal, user)
        except ChatUnavailable:
            await cl.Message(
                content="Chatbot belum aktif — konfigurasi penyedia AI belum diatur."
            ).send()
            return

        conversation = open_chat_session(db, user.id)
        db.commit()

        cl.user_session.set("agent", agent)
        cl.user_session.set("conversation_id", conversation.id)
        cl.user_session.set("history", [])
    finally:
        db.close()

    await cl.Message(content=SAPAAN.format(nama=user.full_name)).send()


@cl.on_message
async def jawab(message: cl.Message) -> None:
    agent = cl.user_session.get("agent")
    if agent is None:
        await cl.Message(content="Sesi belum aktif. Muat ulang halaman ini.").send()
        return

    riwayat = cl.user_session.get("history") or []

    try:
        hasil = await cl.make_async(agent.invoke)(
            {"messages": [*riwayat, HumanMessage(content=message.content)]}
        )
        balasan = _ambil_balasan(hasil)
    except Exception:
        # Kegagalan penyedia AI muncul sebagai pesan ramah, bukan jejak
        # tumpukan yang membingungkan user.
        balasan = PESAN_GAGAL

    _simpan_giliran(message.content, balasan)

    riwayat.extend(
        [HumanMessage(content=message.content), AIMessage(content=balasan)]
    )
    cl.user_session.set("history", riwayat)

    await cl.Message(content=balasan).send()


@cl.on_chat_end
async def selesai() -> None:
    conversation_id = cl.user_session.get("conversation_id")
    if conversation_id is None:
        return

    db = SessionLocal()
    try:
        from app.db.models import ConversationLog

        conversation = db.get(ConversationLog, conversation_id)
        if conversation is not None:
            close_chat_session(db, conversation)
            db.commit()
    finally:
        db.close()


def _token_dari_permintaan() -> str | None:
    """Ambil token dari query string iframe."""
    from app.chat.session import token_from_query

    permintaan = cl.user_session.get("http_referer") or ""
    query = {}
    if "?" in permintaan:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(permintaan).query)

    return token_from_query(query)


def _ambil_balasan(hasil: dict) -> str:
    """Ambil pesan terakhir dari agent."""
    pesan = hasil.get("messages", [])
    terakhir = next((m for m in reversed(pesan) if isinstance(m, AIMessage)), None)
    return str(terakhir.content) if terakhir else PESAN_GAGAL


def _simpan_giliran(pertanyaan: str, jawaban: str) -> None:
    """Catat giliran ke jejak audit.

    Kegagalan menyimpan tidak boleh menghentikan percakapan — user tetap
    menerima jawabannya.
    """
    conversation_id = cl.user_session.get("conversation_id")
    if conversation_id is None:
        return

    db = SessionLocal()
    try:
        from app.db.models import ConversationLog

        conversation = db.get(ConversationLog, conversation_id)
        if conversation is not None:
            record_turn(db, conversation, pertanyaan, jawaban)
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
