"""Status Telegram dan pengiriman notifikasi (PRD FR-5.1 s.d. FR-5.3).

Acceptance criteria under test:
- GET /telegram/status melapor konfigurasi global (bot token + chat id),
  bukan status linking per-akun — tidak ada lagi alur linking (ponytail
  di app/services/telegram.py: hanya mengirim, tidak menerima pesan bot)
- Anomali menulis baris notifications sebelum mencoba mengirim
- Kegagalan kirim menandai status failed dan tidak pernah merambat ke
  jalur deteksi
- Isi pesan memuat metrik, nilai vs baseline, waktu, dan tautan (FR-5.3)
- Semua anomali, dari profil mana pun, terkirim ke satu
  TELEGRAM_DEFAULT_CHAT_ID yang sama (demo/dev, bukan per-akun)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import Anomaly, FamilyMember, Notification
from app.services import telegram as telegram_service
from app.services.notification import notify_anomaly
from app.services.telegram import TelegramDeliveryError
from tests.conftest import make_profile_row


TELEGRAM = "/api/v1/telegram"
DEFAULT_CHAT_ID = "999888777"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch):
    """Setel TELEGRAM_BOT_TOKEN + TELEGRAM_DEFAULT_CHAT_ID untuk satu test."""
    from app.core.config import get_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-uji")
    monkeypatch.setenv("TELEGRAM_DEFAULT_CHAT_ID", DEFAULT_CHAT_ID)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def terkirim(monkeypatch, configured):
    """Tangkap pesan yang dikirim, tanpa menyentuh Telegram sungguhan."""
    pesan: list[dict] = []

    def kirim(chat_id: str, text: str) -> None:
        pesan.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr(telegram_service, "send_message", kirim)
    import app.services.notification as notification_service

    monkeypatch.setattr(notification_service, "send_message", kirim)
    return pesan


@pytest.fixture
def gagal_kirim(monkeypatch, configured):
    def kirim(chat_id: str, text: str) -> None:
        raise TelegramDeliveryError("bot token tidak valid")

    monkeypatch.setattr(telegram_service, "send_message", kirim)
    import app.services.notification as notification_service

    monkeypatch.setattr(notification_service, "send_message", kirim)


def make_anomaly(db, profile: FamilyMember, now: datetime, **overrides) -> Anomaly:
    anomaly = Anomaly(
        family_member_id=profile.id,
        metric_type="heart_rate",
        observed_value=105.0,
        baseline_mean=70.0,
        baseline_stddev=5.0,
        deviation_score=7.0,
        severity="high",
        status="new",
        detected_at=now,
        **overrides,
    )
    db.add(anomaly)
    db.commit()
    return anomaly


# --- Status konfigurasi ------------------------------------------------------


class TestStatus:
    def test_reports_not_configured_by_default(self, client, auth_headers) -> None:
        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_configured"] is False

    def test_reports_configured_when_both_set(
        self, client, auth_headers, configured
    ) -> None:
        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_configured"] is True

    def test_reports_not_configured_when_only_token_set(
        self, client, auth_headers, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-uji")
        get_settings.cache_clear()

        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_configured"] is False
        get_settings.cache_clear()

    def test_same_for_every_account(
        self, client, auth_headers, configured
    ) -> None:
        """Status ini global, bukan per-akun — tidak ada lagi linking."""
        payload = {
            "email": "lain@example.com",
            "password": "rahasia-kuat-123",
            "full_name": "Orang Lain",
        }
        token = client.post("/api/v1/auth/register", json=payload).json()[
            "access_token"
        ]
        other_headers = {"Authorization": f"Bearer {token}"}

        mine = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        theirs = client.get(f"{TELEGRAM}/status", headers=other_headers).json()
        assert mine == theirs

    def test_requires_authentication(self, client) -> None:
        assert client.get(f"{TELEGRAM}/status").status_code == 401


# --- Pengiriman notifikasi ----------------------------------------------------


class TestNotifyAnomaly:
    @pytest.fixture
    def user(self, db_session) -> FamilyMember:
        return make_profile_row(db_session, full_name="Budi")

    def test_writes_notification_row(self, db_session, user, now, terkirim) -> None:
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        rows = db_session.execute(select(Notification)).scalars().all()
        assert len(rows) == 1
        assert rows[0].anomaly_id == anomaly.id

    def test_marks_sent_on_success(self, db_session, user, now, terkirim) -> None:
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        row = db_session.execute(select(Notification)).scalar_one()
        assert row.status == "sent"
        assert row.sent_at is not None

    def test_sends_to_default_chat_id(self, db_session, user, now, terkirim) -> None:
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert terkirim[0]["chat_id"] == DEFAULT_CHAT_ID

    def test_message_contains_required_details(
        self, db_session, user, now, terkirim
    ) -> None:
        """FR-5.3: metrik, nilai vs baseline, waktu, dan tautan."""
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        teks = terkirim[0]["text"]
        assert "105" in teks
        assert "70" in teks
        assert "http" in teks.lower()

    def test_message_names_the_subject(self, db_session, user, now, terkirim) -> None:
        """Satu chat_id dipakai bersama semua akun, jadi nama subjek wajib
        disebut supaya jelas ini soal siapa."""
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert "Budi" in terkirim[0]["text"]

    def test_row_written_before_send_attempt(
        self, db_session, user, now, gagal_kirim
    ) -> None:
        """Baris audit harus ada walau pengiriman gagal — kalau ditulis
        setelahnya, kegagalan berarti tidak ada jejak sama sekali."""
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert db_session.execute(select(Notification)).first() is not None

    def test_failure_marks_status_failed(
        self, db_session, user, now, gagal_kirim
    ) -> None:
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        row = db_session.execute(select(Notification)).scalar_one()
        assert row.status == "failed"

    def test_failure_does_not_raise(self, db_session, user, now, gagal_kirim) -> None:
        """Telegram mati tidak boleh menggagalkan deteksi anomali yang
        sudah tersimpan."""
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)  # tidak boleh melempar

    def test_unconfigured_gets_no_telegram(self, db_session, user, now) -> None:
        """Tanpa TELEGRAM_DEFAULT_CHAT_ID, notifikasi dibiarkan `pending`
        alih-alih dianggap gagal kirim."""
        anomaly = make_anomaly(db_session, user, now)
        notify_anomaly(db_session, anomaly)
        db_session.commit()

        row = db_session.execute(select(Notification)).scalar_one()
        assert row.status == "pending"


class TestSharedChatId:
    """Semua akun berbagi satu TELEGRAM_DEFAULT_CHAT_ID — ponytail yang
    disengaja untuk demo/dev, bukan linking per-akun."""

    def test_every_profile_notifies_same_chat_id(
        self, db_session, now, terkirim
    ) -> None:
        admin = make_profile_row(db_session, full_name="Ayah")
        anak = make_profile_row(db_session, account=admin.account, full_name="Anak")
        lain = make_profile_row(db_session, full_name="Keluarga Lain")
        db_session.commit()

        for profile in (admin, anak, lain):
            notify_anomaly(db_session, make_anomaly(db_session, profile, now))
            db_session.commit()

        assert len(terkirim) == 3
        assert all(p["chat_id"] == DEFAULT_CHAT_ID for p in terkirim)

    def test_single_message_per_anomaly(self, db_session, now, terkirim) -> None:
        person = make_profile_row(db_session, full_name="Mandiri")
        db_session.commit()

        anomaly = make_anomaly(db_session, person, now)
        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert len(terkirim) == 1
