"""Endpoint chat REST, menggantikan UI Chainlit.

Frontend sudah punya komponen chat sendiri (shadcn/ui), jadi backend cukup
menyediakan endpoint JSON — bukan halaman chat utuh.

Acceptance criteria under test:
- POST /chat mengembalikan balasan agent
- Riwayat percakapan dipakai supaya pertanyaan lanjutan nyambung
- Tiap giliran tersimpan ke conversation_log/conversation_messages
- Tanpa API key: 503, bukan error yang membingungkan
- Percakapan orang lain tidak bisa dilanjutkan
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import ConversationLog, ConversationMessage


CHAT = "/api/v1/chat"


@pytest.fixture
def agent_tiruan(monkeypatch):
    """Agent palsu supaya test tidak memanggil DeepSeek sungguhan."""
    from langchain_core.messages import AIMessage

    dipanggil = {}

    class AgentTiruan:
        def invoke(self, payload):
            dipanggil["messages"] = payload["messages"]
            return {"messages": [AIMessage(content="Detak jantungmu rata-rata 72 bpm.")]}

    import app.services.chat as chat_service

    monkeypatch.setattr(chat_service, "build_agent", lambda *a, **k: AgentTiruan())
    return dipanggil


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "kunci-uji")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Mengirim pesan --------------------------------------------------------


class TestSendMessage:
    def test_returns_reply(self, client, auth_headers, agent_tiruan, api_key) -> None:
        response = client.post(
            CHAT, json={"message": "gimana detak jantung saya?"}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        assert "72 bpm" in response.json()["reply"]

    def test_returns_conversation_id(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        """Frontend menyimpan id ini untuk melanjutkan percakapan."""
        body = client.post(
            CHAT, json={"message": "halo"}, headers=auth_headers
        ).json()
        assert uuid.UUID(body["conversation_id"])

    def test_creates_conversation_row(
        self, client, auth_headers, agent_tiruan, api_key, db_session
    ) -> None:
        client.post(CHAT, json={"message": "halo"}, headers=auth_headers)
        assert db_session.execute(select(ConversationLog)).scalar_one()

    def test_saves_both_sides(
        self, client, auth_headers, agent_tiruan, api_key, db_session
    ) -> None:
        client.post(CHAT, json={"message": "halo"}, headers=auth_headers)
        pesan = (
            db_session.execute(
                select(ConversationMessage).order_by(ConversationMessage.sequence)
            )
            .scalars()
            .all()
        )
        assert [m.role for m in pesan] == ["user", "assistant"]

    def test_empty_message_rejected(self, client, auth_headers, api_key) -> None:
        response = client.post(CHAT, json={"message": "   "}, headers=auth_headers)
        assert response.status_code == 422

    def test_requires_authentication(self, client) -> None:
        assert client.post(CHAT, json={"message": "halo"}).status_code == 401


# --- Melanjutkan percakapan ------------------------------------------------


class TestContinueConversation:
    def test_reuses_conversation(
        self, client, auth_headers, agent_tiruan, api_key, db_session
    ) -> None:
        pertama = client.post(
            CHAT, json={"message": "halo"}, headers=auth_headers
        ).json()["conversation_id"]

        kedua = client.post(
            CHAT,
            json={"message": "lanjut", "conversation_id": pertama},
            headers=auth_headers,
        ).json()["conversation_id"]

        assert kedua == pertama
        assert len(db_session.execute(select(ConversationLog)).scalars().all()) == 1

    def test_history_sent_to_agent(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        """Tanpa riwayat, pertanyaan lanjutan seperti "kalau minggu lalu?"
        kehilangan konteks."""
        conv = client.post(
            CHAT, json={"message": "detak jantung saya?"}, headers=auth_headers
        ).json()["conversation_id"]

        client.post(
            CHAT,
            json={"message": "kalau minggu lalu?", "conversation_id": conv},
            headers=auth_headers,
        )

        isi = [str(m.content) for m in agent_tiruan["messages"]]
        assert any("detak jantung saya?" in c for c in isi)

    def test_cannot_continue_other_users_conversation(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        """Isi chat tidak pernah terlihat orang lain."""
        conv = client.post(
            CHAT, json={"message": "rahasia"}, headers=auth_headers
        ).json()["conversation_id"]

        lain = client.post(
            "/api/v1/auth/register",
            json={
                "email": "lain@example.com",
                "password": "rahasia-kuat-123",
                "full_name": "Orang Lain",
            },
        ).json()["access_token"]

        response = client.post(
            CHAT,
            json={"message": "intip", "conversation_id": conv},
            headers={"Authorization": f"Bearer {lain}"},
        )
        assert response.status_code in (403, 404)

    def test_unknown_conversation_rejected(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        response = client.post(
            CHAT,
            json={"message": "halo", "conversation_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404


# --- Chatbot nonaktif ------------------------------------------------------


class TestChatUnavailable:
    def test_missing_api_key_returns_503(
        self, client, auth_headers, monkeypatch
    ) -> None:
        """Chatbot tambahan, bukan syarat: tanpa API key endpoint ini mati
        dengan pesan jelas sementara API lain tetap jalan."""
        from app.core.config import get_settings

        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        get_settings.cache_clear()

        response = client.post(CHAT, json={"message": "halo"}, headers=auth_headers)
        assert response.status_code == 503
        get_settings.cache_clear()

    def test_agent_failure_returns_friendly_error(
        self, client, auth_headers, api_key, monkeypatch
    ) -> None:
        """Kegagalan penyedia AI tidak boleh muncul sebagai jejak tumpukan."""
        import app.services.chat as chat_service

        class AgentMeledak:
            def invoke(self, payload):
                raise RuntimeError("API timeout")

        monkeypatch.setattr(chat_service, "build_agent", lambda *a, **k: AgentMeledak())

        response = client.post(CHAT, json={"message": "halo"}, headers=auth_headers)
        assert response.status_code == 503
        assert "timeout" not in response.json()["detail"].lower()


# --- Riwayat percakapan ----------------------------------------------------


class TestConversationHistory:
    def test_lists_own_conversations(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        client.post(CHAT, json={"message": "halo"}, headers=auth_headers)
        body = client.get(f"{CHAT}/conversations", headers=auth_headers).json()
        assert body["total"] == 1

    def test_reads_own_messages(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        conv = client.post(
            CHAT, json={"message": "halo"}, headers=auth_headers
        ).json()["conversation_id"]

        body = client.get(f"{CHAT}/conversations/{conv}", headers=auth_headers).json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    def test_cannot_read_other_users_messages(
        self, client, auth_headers, agent_tiruan, api_key
    ) -> None:
        conv = client.post(
            CHAT, json={"message": "rahasia"}, headers=auth_headers
        ).json()["conversation_id"]

        lain = client.post(
            "/api/v1/auth/register",
            json={
                "email": "lain2@example.com",
                "password": "rahasia-kuat-123",
                "full_name": "Orang Lain",
            },
        ).json()["access_token"]

        response = client.get(
            f"{CHAT}/conversations/{conv}",
            headers={"Authorization": f"Bearer {lain}"},
        )
        assert response.status_code in (403, 404)
