"""Task 19: agent DeepSeek dengan pengaman medis (PRD FR-4.4).

Acceptance criteria under test:
- Model dibangun lewat pabrik yang provider-agnostic (PRD §11)
- API key kosong gagal jelas saat dipakai, bukan di tengah percakapan
- Prompt sistem melarang diagnosis, resep dosis, dan mengarang angka
- Kegagalan API muncul sebagai pesan ramah, bukan jejak tumpukan
"""

from __future__ import annotations

import pytest

from app.chat.agent import SYSTEM_PROMPT, build_agent
from app.chat.llm import ChatUnavailable, get_chat_model


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "kunci-uji")
    monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- Pabrik model ----------------------------------------------------------


class TestChatModelFactory:
    def test_missing_key_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kunci kosong harus gagal saat model dibuat, bukan setelah user
        mengetik pertanyaan panjang."""
        from app.core.config import get_settings

        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
        get_settings.cache_clear()

        with pytest.raises(ChatUnavailable):
            get_chat_model()
        get_settings.cache_clear()

    def test_uses_configured_provider(self, api_key) -> None:
        """Provider bisa ditukar lewat setting, tanpa menyentuh kode —
        PRD §11 mencatat ketergantungan satu penyedia LLM sebagai risiko."""
        model = get_chat_model()
        assert "deepseek" in str(model.openai_api_base).lower()

    def test_provider_is_swappable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setenv("DEEPSEEK_API_KEY", "kunci-uji")
        monkeypatch.setenv("LLM_BASE_URL", "https://contoh-provider-lain.test")
        monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
        get_settings.cache_clear()

        model = get_chat_model()
        assert "contoh-provider-lain" in str(model.openai_api_base)
        get_settings.cache_clear()

    def test_temperature_defaults_low(self, api_key) -> None:
        """Jawaban soal data kesehatan harus stabil, bukan bervariasi tiap
        kali ditanya hal yang sama."""
        assert get_chat_model().temperature <= 0.3


# --- Pengaman medis --------------------------------------------------------


class TestMedicalGuardrails:
    @pytest.mark.parametrize(
        "larangan",
        ["diagnos", "dosis", "obat", "tenaga medis"],
    )
    def test_prompt_covers_prohibitions(self, larangan: str) -> None:
        """FR-4.4: tidak mendiagnosis, tidak meresepkan dosis, mengarahkan
        ke tenaga medis untuk keluhan serius."""
        assert larangan in SYSTEM_PROMPT.lower()

    def test_prompt_forbids_inventing_numbers(self) -> None:
        """Angka kesehatan yang dikarang lebih berbahaya daripada tidak
        menjawab sama sekali."""
        prompt = SYSTEM_PROMPT.lower()
        assert "karang" in prompt or "mengarang" in prompt

    def test_prompt_requires_tool_use(self) -> None:
        assert "tool" in SYSTEM_PROMPT.lower()

    def test_prompt_mentions_disclaimer(self) -> None:
        """Disclaimer non-diagnostik konsisten di semua titik sentuh
        (PRD §6.2)."""
        prompt = SYSTEM_PROMPT.lower()
        assert "wellness" in prompt or "bukan diagnosis" in prompt

    def test_prompt_is_indonesian(self) -> None:
        assert "kamu" in SYSTEM_PROMPT.lower() or "anda" in SYSTEM_PROMPT.lower()


# --- Perakitan agent -------------------------------------------------------


class TestBuildAgent:
    def test_builds_with_tools(self, api_key, db_session) -> None:
        from app.db.models import User

        user = User(full_name="Budi", email="budi@example.com")
        db_session.add(user)
        db_session.commit()

        agent = build_agent(lambda: db_session, user)
        assert agent is not None

    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch, db_session) -> None:
        from app.core.config import get_settings
        from app.db.models import User

        monkeypatch.setenv("DEEPSEEK_API_KEY", "")
        monkeypatch.setenv("JWT_SECRET", "test-secret-yang-cukup-panjang-untuk-hmac")
        get_settings.cache_clear()

        user = User(full_name="Budi", email="budi@example.com")
        db_session.add(user)
        db_session.commit()

        with pytest.raises(ChatUnavailable):
            build_agent(lambda: db_session, user)
        get_settings.cache_clear()
