"""Task 21: sesi chat Chainlit (PRD FR-4.5, A2).

Chainlit berjalan sebagai proses terpisah dan di-embed sebagai iframe.
Yang diuji di sini adalah lapisan sesi yang dipakainya — bukan UI-nya,
yang tidak bisa diuji tanpa browser.

Acceptance criteria under test:
- Token diverifikasi dengan security.py yang sama dengan REST API
- Token tidak valid menolak sesi, bukan membuka sesi anonim
- Tiap giliran tersimpan ke conversation_log/conversation_messages
- Menutup sesi mengisi ended_at
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.chat.session import (
    ChatAuthError,
    authenticate_token,
    close_chat_session,
    open_chat_session,
    record_turn,
)
from app.core.security import create_access_token, hash_password
from app.db.models import ConversationLog, ConversationMessage, User


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "secret-khusus-test-yang-cukup-panjang-32b")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "kunci-uji")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def user(db_session) -> User:
    person = User(
        full_name="Budi",
        email="budi@example.com",
        password_hash=hash_password("rahasia"),
    )
    db_session.add(person)
    db_session.commit()
    return person


# --- Autentikasi -----------------------------------------------------------


class TestAuthenticateToken:
    def test_valid_token_returns_user(self, db_session, user, secret) -> None:
        """Token yang sama dengan REST API — user tidak perlu login dua kali
        saat berpindah ke tab chat (PRD A2)."""
        resolved = authenticate_token(db_session, create_access_token(user.id))
        assert resolved.id == user.id

    def test_missing_token_refused(self, db_session, secret) -> None:
        """Tanpa token, sesi ditolak — bukan dibuka sebagai anonim."""
        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, None)

    def test_empty_token_refused(self, db_session, secret) -> None:
        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, "")

    def test_invalid_token_refused(self, db_session, secret) -> None:
        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, "token-palsu")

    def test_expired_token_refused(self, db_session, user, secret) -> None:
        kedaluwarsa = create_access_token(
            user.id, expires_delta=timedelta(seconds=-1)
        )
        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, kedaluwarsa)

    def test_token_for_deleted_user_refused(self, db_session, secret) -> None:
        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, create_access_token(uuid.uuid4()))

    def test_deactivated_user_refused(self, db_session, user, secret) -> None:
        """User yang dinonaktifkan langsung kehilangan akses chat, sama
        seperti di REST API."""
        token = create_access_token(user.id)
        user.is_active = False
        db_session.commit()

        with pytest.raises(ChatAuthError):
            authenticate_token(db_session, token)


# --- Siklus sesi -----------------------------------------------------------


class TestSessionLifecycle:
    def test_open_creates_conversation(self, db_session, user, secret) -> None:
        conversation = open_chat_session(db_session, user.id)
        db_session.commit()
        assert conversation.user_id == user.id
        assert db_session.execute(select(ConversationLog)).scalar_one()

    def test_record_turn_saves_both_sides(self, db_session, user, secret) -> None:
        """Satu giliran = pertanyaan user plus jawaban bot, tersimpan
        berurutan sebagai jejak audit."""
        conversation = open_chat_session(db_session, user.id)
        record_turn(db_session, conversation, "gimana detak jantung saya?", "72 bpm")
        db_session.commit()

        pesan = (
            db_session.execute(
                select(ConversationMessage).order_by(ConversationMessage.sequence)
            )
            .scalars()
            .all()
        )
        assert [(m.role, m.content) for m in pesan] == [
            ("user", "gimana detak jantung saya?"),
            ("assistant", "72 bpm"),
        ]

    def test_multiple_turns_keep_order(self, db_session, user, secret) -> None:
        conversation = open_chat_session(db_session, user.id)
        for i in range(3):
            record_turn(db_session, conversation, f"tanya {i}", f"jawab {i}")
        db_session.commit()

        pesan = (
            db_session.execute(
                select(ConversationMessage).order_by(ConversationMessage.sequence)
            )
            .scalars()
            .all()
        )
        assert [m.content for m in pesan] == [
            "tanya 0",
            "jawab 0",
            "tanya 1",
            "jawab 1",
            "tanya 2",
            "jawab 2",
        ]

    def test_close_marks_ended(self, db_session, user, secret) -> None:
        conversation = open_chat_session(db_session, user.id)
        record_turn(db_session, conversation, "halo", "halo juga")
        close_chat_session(db_session, conversation)
        db_session.commit()
        assert conversation.ended_at is not None

    def test_close_without_turns_is_safe(self, db_session, user, secret) -> None:
        """User membuka tab chat lalu menutupnya tanpa bertanya."""
        conversation = open_chat_session(db_session, user.id)
        close_chat_session(db_session, conversation)
        db_session.commit()
        assert conversation.ended_at is not None

    def test_empty_reply_still_recorded(self, db_session, user, secret) -> None:
        """Jawaban kosong dari model tetap dicatat sebagai penanda, bukan
        membuat giliran user ikut hilang dari audit."""
        conversation = open_chat_session(db_session, user.id)
        record_turn(db_session, conversation, "tanya", "")
        db_session.commit()

        pesan = db_session.execute(select(ConversationMessage)).scalars().all()
        assert len(pesan) >= 1
        assert pesan[0].content == "tanya"


# --- Ekstraksi token dari URL ----------------------------------------------


class TestTokenExtraction:
    """Token dikirim frontend sebagai query param iframe (PRD A2)."""

    def test_reads_token_from_query(self) -> None:
        from app.chat.session import token_from_query

        assert token_from_query({"token": ["abc123"]}) == "abc123"

    def test_handles_plain_string_value(self) -> None:
        from app.chat.session import token_from_query

        assert token_from_query({"token": "abc123"}) == "abc123"

    def test_missing_token_returns_none(self) -> None:
        from app.chat.session import token_from_query

        assert token_from_query({}) is None

    def test_empty_list_returns_none(self) -> None:
        from app.chat.session import token_from_query

        assert token_from_query({"token": []}) is None
