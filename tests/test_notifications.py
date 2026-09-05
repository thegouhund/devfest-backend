"""Task 15: linking Telegram dan pengiriman notifikasi (PRD FR-5.1 s.d. FR-5.3).

Acceptance criteria under test:
- POST /telegram/link mengembalikan kode sekali pakai yang kedaluwarsa
- Mengirim kode ke bot menyambungkan akun; kode terpakai/kedaluwarsa ditolak
- Anomali menulis baris notifications sebelum mencoba mengirim
- Kegagalan kirim menandai status failed dan tidak pernah merambat ke
  jalur deteksi
- Isi pesan memuat metrik, nilai vs baseline, waktu, dan tautan (FR-5.3)
- Anomali pada dependent memberi tahu admin pengelolanya juga (FR-5.2)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Anomaly, Notification, TelegramLink, User
from app.services import telegram as telegram_service
from app.services.notification import notify_anomaly
from app.services.telegram import (
    LINK_CODE_TTL,
    TelegramDeliveryError,
    consume_link_code,
    issue_link_code,
)


TELEGRAM = "/api/v1/telegram"
FAMILIES = "/api/v1/families"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def terkirim(monkeypatch):
    """Tangkap pesan yang dikirim, tanpa menyentuh Telegram sungguhan."""
    pesan: list[dict] = []

    def kirim(chat_id: str, text: str) -> None:
        pesan.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr(telegram_service, "send_message", kirim)
    import app.services.notification as notification_service

    monkeypatch.setattr(notification_service, "send_message", kirim)
    return pesan


@pytest.fixture
def gagal_kirim(monkeypatch):
    def kirim(chat_id: str, text: str) -> None:
        raise TelegramDeliveryError("bot token tidak valid")

    monkeypatch.setattr(telegram_service, "send_message", kirim)
    import app.services.notification as notification_service

    monkeypatch.setattr(notification_service, "send_message", kirim)


def link_user(db, user: User, chat_id: str = "12345") -> TelegramLink:
    link = TelegramLink(
        user_id=user.id, telegram_chat_id=chat_id, link_code=None, is_active=True
    )
    db.add(link)
    db.commit()
    return link


def make_anomaly(db, user: User, now: datetime, **overrides) -> Anomaly:
    anomaly = Anomaly(
        user_id=user.id,
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


# --- Penerbitan kode -------------------------------------------------------


class TestIssueLinkCode:
    def test_returns_code_and_expiry(self, client, auth_headers) -> None:
        response = client.post(f"{TELEGRAM}/link", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["link_code"]
        assert body["expires_at"]

    def test_code_is_not_guessable(self, client, auth_headers) -> None:
        """Kode yang bisa ditebak berarti orang lain bisa mengalihkan
        notifikasi kesehatan ke akun Telegram-nya."""
        kode = {
            client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
            for _ in range(20)
        }
        assert len(kode) == 20
        assert all(len(k) >= 6 for k in kode)

    def test_reissue_replaces_previous(self, db_session, auth_headers, client) -> None:
        """Meminta kode baru membatalkan yang lama, supaya tidak ada dua
        kode aktif sekaligus."""
        lama = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        client.post(f"{TELEGRAM}/link", headers=auth_headers)

        with pytest.raises(LookupError):
            consume_link_code(db_session, lama, chat_id="999")

    def test_requires_authentication(self, client) -> None:
        assert client.post(f"{TELEGRAM}/link").status_code == 401


# --- Menukarkan kode -------------------------------------------------------


class TestConsumeLinkCode:
    def test_valid_code_links_account(self, db_session, client, auth_headers) -> None:
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        link = consume_link_code(db_session, kode, chat_id="55555")
        db_session.commit()

        assert link.telegram_chat_id == "55555"
        assert link.is_active is True

    def test_code_cleared_after_use(self, db_session, client, auth_headers) -> None:
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        link = consume_link_code(db_session, kode, chat_id="55555")
        db_session.commit()
        assert link.link_code is None

    def test_code_cannot_be_reused(self, db_session, client, auth_headers) -> None:
        """Sekali pakai: kode yang sudah ditukarkan tidak boleh menyambungkan
        akun Telegram kedua."""
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        consume_link_code(db_session, kode, chat_id="55555")
        db_session.commit()

        with pytest.raises(LookupError):
            consume_link_code(db_session, kode, chat_id="66666")

    def test_expired_code_rejected(self, db_session, client, auth_headers) -> None:
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        link = db_session.execute(select(TelegramLink)).scalar_one()
        link.link_code_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db_session.commit()

        with pytest.raises(LookupError):
            consume_link_code(db_session, kode, chat_id="55555")

    def test_unknown_code_rejected(self, db_session) -> None:
        with pytest.raises(LookupError):
            consume_link_code(db_session, "TIDAKADA", chat_id="55555")

    def test_ttl_is_named_constant(self) -> None:
        assert LINK_CODE_TTL > timedelta(0)


# --- Status & pemutusan ----------------------------------------------------


class TestStatus:
    def test_reports_not_linked(self, client, auth_headers) -> None:
        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_linked"] is False

    def test_reports_linked(self, client, auth_headers, db_session) -> None:
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        consume_link_code(db_session, kode, chat_id="55555")
        db_session.commit()

        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_linked"] is True
        assert body["linked_at"]

    def test_pending_code_is_not_linked(self, client, auth_headers) -> None:
        """Kode sudah diminta tapi belum ditukarkan: belum tersambung."""
        client.post(f"{TELEGRAM}/link", headers=auth_headers)
        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_linked"] is False

    def test_unlink(self, client, auth_headers, db_session) -> None:
        kode = client.post(f"{TELEGRAM}/link", headers=auth_headers).json()["link_code"]
        consume_link_code(db_session, kode, chat_id="55555")
        db_session.commit()

        assert client.delete(f"{TELEGRAM}/link", headers=auth_headers).status_code == 204
        body = client.get(f"{TELEGRAM}/status", headers=auth_headers).json()
        assert body["is_linked"] is False


# --- Pengiriman notifikasi -------------------------------------------------


class TestNotifyAnomaly:
    @pytest.fixture
    def user(self, db_session) -> User:
        person = User(full_name="Budi", email="budi@example.com")
        db_session.add(person)
        db_session.commit()
        return person

    def test_writes_notification_row(
        self, db_session, user, now, terkirim
    ) -> None:
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        rows = db_session.execute(select(Notification)).scalars().all()
        assert len(rows) == 1
        assert rows[0].anomaly_id == anomaly.id

    def test_marks_sent_on_success(self, db_session, user, now, terkirim) -> None:
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        row = db_session.execute(select(Notification)).scalar_one()
        assert row.status == "sent"
        assert row.sent_at is not None

    def test_message_contains_required_details(
        self, db_session, user, now, terkirim
    ) -> None:
        """FR-5.3: metrik, nilai vs baseline, waktu, dan tautan."""
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        teks = terkirim[0]["text"]
        assert "105" in teks
        assert "70" in teks
        assert "http" in teks.lower()

    def test_row_written_before_send_attempt(
        self, db_session, user, now, gagal_kirim
    ) -> None:
        """Baris audit harus ada walau pengiriman gagal — kalau ditulis
        setelahnya, kegagalan berarti tidak ada jejak sama sekali."""
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert db_session.execute(select(Notification)).first() is not None

    def test_failure_marks_status_failed(
        self, db_session, user, now, gagal_kirim
    ) -> None:
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        row = db_session.execute(select(Notification)).scalar_one()
        assert row.status == "failed"

    def test_failure_does_not_raise(self, db_session, user, now, gagal_kirim) -> None:
        """Telegram mati tidak boleh menggagalkan deteksi anomali yang
        sudah tersimpan."""
        link_user(db_session, user)
        anomaly = make_anomaly(db_session, user, now)

        notify_anomaly(db_session, anomaly)  # tidak boleh melempar

    def test_unlinked_user_gets_no_telegram(
        self, db_session, user, now, terkirim
    ) -> None:
        anomaly = make_anomaly(db_session, user, now)
        notify_anomaly(db_session, anomaly)
        db_session.commit()
        assert terkirim == []

    def test_inactive_link_skipped(self, db_session, user, now, terkirim) -> None:
        link = link_user(db_session, user)
        link.is_active = False
        db_session.commit()

        anomaly = make_anomaly(db_session, user, now)
        notify_anomaly(db_session, anomaly)
        db_session.commit()
        assert terkirim == []


class TestDependentNotification:
    """FR-5.2: anomali pada dependent juga memberi tahu admin pengelolanya,
    karena dependent tidak punya akun Telegram sendiri."""

    @pytest.fixture
    def keluarga(self, db_session):
        admin = User(full_name="Ayah", email="ayah@example.com")
        db_session.add(admin)
        db_session.flush()
        anak = User(
            full_name="Anak", is_dependent=True, managed_by_user_id=admin.id
        )
        db_session.add(anak)
        db_session.commit()
        return {"admin": admin, "anak": anak}

    def test_manager_notified(self, db_session, keluarga, now, terkirim) -> None:
        link_user(db_session, keluarga["admin"], chat_id="admin-chat")
        anomaly = make_anomaly(db_session, keluarga["anak"], now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert any(p["chat_id"] == "admin-chat" for p in terkirim)

    def test_message_names_the_subject(
        self, db_session, keluarga, now, terkirim
    ) -> None:
        """Admin perlu tahu ini soal siapa — dia bisa mengelola beberapa
        dependent sekaligus."""
        link_user(db_session, keluarga["admin"], chat_id="admin-chat")
        anomaly = make_anomaly(db_session, keluarga["anak"], now)

        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert "Anak" in terkirim[0]["text"]

    def test_no_duplicate_when_subject_is_own_manager(
        self, db_session, now, terkirim
    ) -> None:
        """User biasa tidak boleh dapat dua pesan untuk satu anomali."""
        person = User(full_name="Mandiri", email="mandiri@example.com")
        db_session.add(person)
        db_session.commit()
        link_user(db_session, person, chat_id="chat-sendiri")

        anomaly = make_anomaly(db_session, person, now)
        notify_anomaly(db_session, anomaly)
        db_session.commit()

        assert len(terkirim) == 1
