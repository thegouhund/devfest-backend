"""Pabrik model bahasa untuk health companion.

DeepSeek dipakai lewat antarmuka yang kompatibel dengan OpenAI, jadi
berpindah penyedia cukup mengganti `LLM_BASE_URL` dan `LLM_MODEL` tanpa
menyentuh kode — PRD §11 mencatat ketergantungan pada satu penyedia LLM
sebagai risiko yang perlu dimitigasi.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.core.config import get_settings


class ChatUnavailable(RuntimeError):
    """Chatbot tidak bisa dipakai karena API key belum diatur.

    Dipakai pemanggil untuk menjawab 503: chatbot adalah tambahan, bukan
    syarat — dashboard dan pengukuran tetap harus jalan tanpanya.
    """


# Rendah supaya jawaban soal data kesehatan stabil: pertanyaan yang sama
# tidak boleh menghasilkan angka atau kesimpulan yang berbeda-beda.
DEFAULT_TEMPERATURE = 0.1


def get_chat_model(temperature: float = DEFAULT_TEMPERATURE) -> ChatOpenAI:
    settings = get_settings()

    if not settings.deepseek_api_key:
        # Gagal di sini, bukan setelah user mengetik pertanyaan panjang.
        raise ChatUnavailable(
            "DEEPSEEK_API_KEY belum diatur, chatbot tidak bisa dipakai"
        )

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.llm_base_url,
        temperature=temperature,
    )
