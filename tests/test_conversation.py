"""Task 17: penyimpanan percakapan chatbot (ERD §2.11, §2.12).

Acceptance criteria under test:
- Memulai chat membuat baris conversation_log
- Tiap giliran user/assistant/system/tool tersimpan berurutan
- Mengakhiri sesi mengisi ended_at dan ringkasan
- Pengambilan riwayat dibatasi hanya pemiliknya

Catatan: pesan chat memuat cerita kesehatan yang sangat pribadi. Berbeda
dari vitals yang bisa dibagikan ke keluarga, isi percakapan tidak pernah
terlihat anggota lain — bahkan admin yang mengelola dependent sekalipun.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import ConversationLog, ConversationMessage, User
from app.services.conversation import (
    NotConversationOwner,
    append_message,
    end_conversation,
    get_history,
    start_conversation,
)


@pytest.fixture
def user(db_session) -> User:
    person = User(full_name="Budi", email="budi@example.com")
    db_session.add(person)
    db_session.commit()
    return person


@pytest.fixture
def other_user(db_session) -> User:
    person = User(full_name="Siti", email="siti@example.com")
    db_session.add(person)
    db_session.commit()
    return person


# --- Memulai percakapan ----------------------------------------------------


class TestStartConversation:
    def test_creates_log_row(self, db_session, user) -> None:
        conversation = start_conversation(db_session, user.id)
        db_session.commit()

        assert conversation.user_id == user.id
        assert db_session.execute(select(ConversationLog)).scalar_one() is not None

    def test_records_start_time(self, db_session, user) -> None:
        conversation = start_conversation(db_session, user.id)
        db_session.commit()
        assert conversation.started_at is not None

    def test_starts_unfinished(self, db_session, user) -> None:
        conversation = start_conversation(db_session, user.id)
        db_session.commit()
        assert conversation.ended_at is None
        assert conversation.summary is None

    def test_multiple_conversations_allowed(self, db_session, user) -> None:
        """User bisa punya beberapa sesi chat pada waktu berbeda."""
        start_conversation(db_session, user.id)
        start_conversation(db_session, user.id)
        db_session.commit()
        rows = db_session.execute(select(ConversationLog)).scalars().all()
        assert len(rows) == 2


# --- Menambah pesan --------------------------------------------------------


class TestAppendMessage:
    @pytest.fixture
    def conversation(self, db_session, user) -> ConversationLog:
        row = start_conversation(db_session, user.id)
        db_session.commit()
        return row

    @pytest.mark.parametrize("role", ["user", "assistant", "system", "tool"])
    def test_all_erd_roles_accepted(self, db_session, conversation, role) -> None:
        message = append_message(db_session, conversation, role, "isi pesan")
        db_session.commit()
        assert message.role == role

    def test_unknown_role_rejected(self, db_session, conversation) -> None:
        with pytest.raises(ValueError):
            append_message(db_session, conversation, "dukun", "isi")

    def test_preserves_order(self, db_session, conversation) -> None:
        """Urutan giliran menentukan makna percakapan — kalau teracak,
        jawaban bot bisa terbaca sebagai pertanyaan user."""
        giliran = [
            ("user", "gimana detak jantung saya?"),
            ("assistant", "rata-rata 72 bpm minggu ini"),
            ("user", "normal?"),
            ("assistant", "masih dalam rentang biasa Anda"),
        ]
        for role, content in giliran:
            append_message(db_session, conversation, role, content)
        db_session.commit()

        history = get_history(db_session, conversation.id, viewer_id=conversation.user_id)
        assert [(m.role, m.content) for m in history] == giliran

    def test_order_survives_identical_timestamps(
        self, db_session, conversation
    ) -> None:
        """Beberapa giliran sering tertulis dalam detik yang sama. Tanpa
        nomor urut eksplisit, urutannya jatuh ke id yang berupa UUID acak —
        jawaban bot bisa muncul sebelum pertanyaannya."""
        stamp = datetime.now(UTC)
        giliran = [
            ("user", "pertama"),
            ("assistant", "kedua"),
            ("user", "ketiga"),
            ("assistant", "keempat"),
        ]
        for role, content in giliran:
            message = append_message(db_session, conversation, role, content)
            message.created_at = stamp
        db_session.commit()

        history = get_history(
            db_session, conversation.id, viewer_id=conversation.user_id
        )
        assert [(m.role, m.content) for m in history] == giliran

    def test_sequence_increments(self, db_session, conversation) -> None:
        for teks in ("satu", "dua", "tiga"):
            append_message(db_session, conversation, "user", teks)
        db_session.commit()

        history = get_history(
            db_session, conversation.id, viewer_id=conversation.user_id
        )
        assert [m.sequence for m in history] == [0, 1, 2]

    def test_sequence_independent_per_conversation(
        self, db_session, conversation, user
    ) -> None:
        """Nomor urut dihitung per percakapan, bukan global."""
        append_message(db_session, conversation, "user", "di sesi pertama")
        lain = start_conversation(db_session, user.id)
        pesan = append_message(db_session, lain, "user", "di sesi kedua")
        db_session.commit()
        assert pesan.sequence == 0

    def test_empty_content_rejected(self, db_session, conversation) -> None:
        with pytest.raises(ValueError):
            append_message(db_session, conversation, "user", "   ")

    def test_message_linked_to_conversation(self, db_session, conversation) -> None:
        append_message(db_session, conversation, "user", "halo")
        db_session.commit()
        message = db_session.execute(select(ConversationMessage)).scalar_one()
        assert message.conversation_id == conversation.id

    def test_cannot_append_to_ended_conversation(
        self, db_session, conversation
    ) -> None:
        """Sesi yang sudah ditutup dan diringkas tidak boleh bertambah —
        ringkasannya akan jadi tidak cocok dengan isinya."""
        end_conversation(db_session, conversation, summary="selesai")
        db_session.commit()

        with pytest.raises(ValueError):
            append_message(db_session, conversation, "user", "satu lagi")


# --- Mengakhiri percakapan -------------------------------------------------


class TestEndConversation:
    @pytest.fixture
    def conversation(self, db_session, user) -> ConversationLog:
        row = start_conversation(db_session, user.id)
        append_message(db_session, row, "user", "halo")
        db_session.commit()
        return row

    def test_sets_end_time(self, db_session, conversation) -> None:
        end_conversation(db_session, conversation, summary="obrolan singkat")
        db_session.commit()
        assert conversation.ended_at is not None

    def test_stores_summary(self, db_session, conversation) -> None:
        end_conversation(db_session, conversation, summary="obrolan singkat")
        db_session.commit()
        assert conversation.summary == "obrolan singkat"

    def test_summary_optional(self, db_session, conversation) -> None:
        """Ringkasan dibuat model dan bisa gagal; sesi tetap harus bisa
        ditutup tanpa itu."""
        end_conversation(db_session, conversation, summary=None)
        db_session.commit()
        assert conversation.ended_at is not None
        assert conversation.summary is None

    def test_ending_twice_keeps_first_time(self, db_session, conversation) -> None:
        end_conversation(db_session, conversation, summary="pertama")
        db_session.commit()
        pertama = conversation.ended_at

        end_conversation(db_session, conversation, summary="kedua")
        db_session.commit()
        assert conversation.ended_at == pertama


# --- Akses riwayat ---------------------------------------------------------


class TestHistoryAccess:
    @pytest.fixture
    def conversation(self, db_session, user) -> ConversationLog:
        row = start_conversation(db_session, user.id)
        append_message(db_session, row, "user", "riwayat alergi saya penisilin")
        db_session.commit()
        return row

    def test_owner_can_read(self, db_session, conversation, user) -> None:
        history = get_history(db_session, conversation.id, viewer_id=user.id)
        assert len(history) == 1

    def test_other_user_cannot_read(
        self, db_session, conversation, other_user
    ) -> None:
        """Isi percakapan tidak pernah terlihat orang lain — berbeda dari
        vitals yang bisa dibagikan ke keluarga."""
        with pytest.raises(NotConversationOwner):
            get_history(db_session, conversation.id, viewer_id=other_user.id)

    def test_manager_cannot_read_dependent_chat(self, db_session, user) -> None:
        """Bahkan admin pengelola tidak bisa membaca isi chat dependent-nya.

        Dependent memang tidak login sendiri, tapi aturannya dibuat tegas
        supaya tidak ada jalur baca yang terbuka begitu saja nanti.
        """
        anak = User(full_name="Anak", is_dependent=True, managed_by_user_id=user.id)
        db_session.add(anak)
        db_session.commit()

        percakapan = start_conversation(db_session, anak.id)
        append_message(db_session, percakapan, "user", "cerita pribadi")
        db_session.commit()

        with pytest.raises(NotConversationOwner):
            get_history(db_session, percakapan.id, viewer_id=user.id)

    def test_unknown_conversation_raises(self, db_session, user) -> None:
        with pytest.raises(LookupError):
            get_history(db_session, uuid.uuid4(), viewer_id=user.id)


# --- Daftar percakapan -----------------------------------------------------


class TestListConversations:
    def test_lists_only_own(self, db_session, user, other_user) -> None:
        from app.services.conversation import list_conversations

        start_conversation(db_session, user.id)
        start_conversation(db_session, other_user.id)
        db_session.commit()

        rows = list_conversations(db_session, user.id)
        assert len(rows) == 1
        assert rows[0].user_id == user.id

    def test_newest_first(self, db_session, user) -> None:
        from app.services.conversation import list_conversations

        pertama = start_conversation(db_session, user.id)
        db_session.commit()
        kedua = start_conversation(db_session, user.id)
        kedua.started_at = datetime.now(UTC).replace(year=2027)
        db_session.commit()

        rows = list_conversations(db_session, user.id)
        assert rows[0].id == kedua.id
