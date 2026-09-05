"""Task 18: tools LangChain di atas service yang sudah ada (PRD FR-4.1, FR-4.3).

Acceptance criteria under test:
- Tiap tool memanggil fungsi service yang sama dengan endpoint REST-nya,
  bukan query sendiri
- Id user yang bertindak diikat di server; tidak ada tool yang menerima
  user id dari keluaran model
- Menanyakan anggota family yang tidak boleh dilihat menghasilkan penolakan
  berupa teks, bukan data
- Deskripsi tool cukup spesifik untuk pemilihan yang benar
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.chat.tools import make_tools
from app.db.models import ActivityLog, Anomaly, MeasurementSession, FamilyMember, VitalsReading


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def session_factory(db_session):
    """Tiap tool membuka session sendiri (lihat docstring app/chat/tools.py).

    Di test, semuanya diarahkan ke satu session in-memory karena tiap
    koneksi SQLite in-memory adalah database berbeda.
    """
    return lambda: db_session


@pytest.fixture
def keluarga_dengan_data(keluarga, db_session, now):
    """Fixture `keluarga` bersama (conftest), ditambah riwayat ayah."""
    session = MeasurementSession(
        family_member_id=keluarga["ayah"]["id"],
        initiated_by_family_member_id=keluarga["ayah"]["id"],
        capture_method="upload",
        started_at=now,
        processing_status="completed",
    )
    db_session.add(session)
    db_session.flush()
    for hari in range(5):
        db_session.add(
            VitalsReading(
                measurement_session_id=session.id,
                family_member_id=keluarga["ayah"]["id"],
                recorded_at=now - timedelta(days=hari),
                metric_type="heart_rate",
                value=70 + hari,
                unit="bpm",
            )
        )
    db_session.commit()
    return keluarga


def tools_for(session_factory, db_session, user_id: uuid.UUID) -> dict:
    """Bangun tools untuk satu user, dikembalikan sebagai dict per nama."""
    actor = db_session.get(FamilyMember, user_id)
    return {t.name: t for t in make_tools(session_factory, actor)}


# --- Struktur tools --------------------------------------------------------


class TestToolSet:
    def test_expected_tools_exist(self, session_factory, db_session, keluarga_dengan_data) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        assert {
            "get_vitals_stats",
            "get_recent_activities",
            "get_anomaly_events",
            "log_activity",
            "get_user_profile",
        } <= set(tools)

    def test_every_tool_has_description(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Deskripsi yang kabur membuat model memilih tool yang salah."""
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        for name, tool in tools.items():
            assert tool.description, f"{name} tanpa deskripsi"
            assert len(tool.description) > 40, f"{name} deskripsinya terlalu pendek"

    def test_no_tool_accepts_user_id(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Id pelaku diikat di server. Kalau tool menerima user id dari
        keluaran model, model bisa dibujuk membaca data orang lain."""
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        for name, tool in tools.items():
            fields = set(tool.args_schema.model_fields)
            assert "user_id" not in fields, f"{name} menerima user_id dari model"
            assert "actor_id" not in fields, f"{name} menerima actor_id dari model"


# --- Statistik vital -------------------------------------------------------


class TestGetVitalsStats:
    def test_returns_own_data(self, session_factory, db_session, keluarga_dengan_data) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "heart_rate", "days": 7}
        )
        assert "72" in hasil or "70" in hasil

    def test_unknown_metric_explains(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "kadar_gula", "days": 7}
        )
        assert "kadar_gula" in hasil.lower() or "tidak" in hasil.lower()

    def test_no_data_says_so(self, session_factory, db_session, keluarga_dengan_data) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["luar"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "heart_rate", "days": 7}
        )
        assert "belum" in hasil.lower() or "tidak ada" in hasil.lower()

    def test_family_member_accessible(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ibu"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "heart_rate", "days": 7, "member_name": "Ayah"}
        )
        assert "72" in hasil or "70" in hasil

    def test_private_member_refused(
        self, session_factory, db_session, client, keluarga_dengan_data
    ) -> None:
        """Setelan privasi berlaku sama di chat maupun REST."""
        client.put(
            "/api/v1/settings/visibility",
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ibu"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "heart_rate", "days": 7, "member_name": "Ayah"}
        )
        assert "70" not in hasil and "72" not in hasil

    def test_outsider_cannot_reach_family(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Percobaan membaca data keluarga lain lewat nama harus ditolak
        sebagai teks, bukan mengembalikan angka."""
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["luar"]["id"])
        hasil = tools["get_vitals_stats"].invoke(
            {"metric_type": "heart_rate", "days": 7, "member_name": "Ayah"}
        )
        assert "70" not in hasil and "72" not in hasil


# --- Aktivitas -------------------------------------------------------------


class TestActivityTools:
    def test_log_activity_creates_row(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke(
            {"category": "coffee", "quantity": 2, "unit": "cups"}
        )
        row = db_session.execute(select(ActivityLog)).scalar_one()
        assert row.category == "coffee"

    def test_logged_activity_marked_as_chat(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Sumber `chat` membedakannya dari entri tombol quick-menu (FR-4.3)."""
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke({"category": "coffee", "quantity": 2})
        row = db_session.execute(select(ActivityLog)).scalar_one()
        assert row.source == "chat"

    def test_log_activity_belongs_to_actor(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke({"category": "coffee"})
        row = db_session.execute(select(ActivityLog)).scalar_one()
        assert row.family_member_id == keluarga_dengan_data["ayah"]["id"]

    def test_invalid_category_explains(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Model kadang mengarang kategori; jawabannya harus menuntun,
        bukan melempar exception yang mematikan percakapan."""
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["log_activity"].invoke({"category": "belanja"})
        assert "coffee" in hasil or "kategori" in hasil.lower()
        assert db_session.execute(select(ActivityLog)).first() is None

    def test_recent_activities_lists(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke({"category": "coffee", "quantity": 3})
        hasil = tools["get_recent_activities"].invoke({"days": 7})
        assert "coffee" in hasil.lower() or "kopi" in hasil.lower()

    def test_quantity_has_no_excess_precision(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        """Kolom Numeric Postgres mengembalikan Decimal, dan format :g pada
        Decimal mempertahankan nol di belakang koma. Tanpa penanganan, model
        akan menyalin "2.0000000000 cups" ke jawabannya."""
        from decimal import Decimal

        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke({"category": "coffee", "quantity": 2})

        row = db_session.execute(select(ActivityLog)).scalar_one()
        row.quantity = Decimal("2.0000000000")
        db_session.commit()

        hasil = tools["get_recent_activities"].invoke({"days": 7})
        assert "2.0000" not in hasil
        assert "2 " in hasil or "2." not in hasil

    def test_recent_activities_empty(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_recent_activities"].invoke({"days": 7})
        assert "belum" in hasil.lower() or "tidak ada" in hasil.lower()


# --- Anomali ---------------------------------------------------------------


class TestAnomalyTool:
    def test_lists_anomalies(self, session_factory, db_session, keluarga_dengan_data, now) -> None:
        db_session.add(
            Anomaly(
                family_member_id=keluarga_dengan_data["ayah"]["id"],
                metric_type="heart_rate",
                observed_value=105.0,
                baseline_mean=70.0,
                baseline_stddev=5.0,
                deviation_score=7.0,
                severity="high",
                status="new",
                detected_at=now,
            )
        )
        db_session.commit()

        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_anomaly_events"].invoke({"days": 30})
        assert "105" in hasil

    def test_no_anomalies_says_so(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_anomaly_events"].invoke({"days": 30})
        assert "tidak ada" in hasil.lower() or "belum" in hasil.lower()


# --- Profil ----------------------------------------------------------------


class TestProfileTool:
    def test_returns_physical_context(
        self, session_factory, db_session, client, keluarga_dengan_data
    ) -> None:
        """Tinggi & berat jadi konteks analisis chatbot (FR-4.1)."""
        client.patch(
            f"/api/v1/profiles/{keluarga_dengan_data['ayah']['id']}",
            json={"height_cm": 170, "weight": 65},
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_user_profile"].invoke({})
        assert "170" in hasil and "65" in hasil

    def test_missing_profile_does_not_crash(
        self, session_factory, db_session, keluarga_dengan_data
    ) -> None:
        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        hasil = tools["get_user_profile"].invoke({})
        assert "Ayah" in hasil


# --- Penggunaan ulang service ----------------------------------------------


class TestServiceReuse:
    def test_vitals_tool_uses_statistics_service(
        self, session_factory, db_session, keluarga_dengan_data, monkeypatch
    ) -> None:
        """Tool tidak boleh query/hitung sendiri — angka di chat harus
        selalu sama dengan yang tampil di dashboard."""
        dipanggil = []
        import app.chat.tools as chat_tools

        asli = chat_tools.statistics.aggregate

        def catat(*args, **kwargs):
            dipanggil.append(True)
            return asli(*args, **kwargs)

        monkeypatch.setattr(chat_tools.statistics, "aggregate", catat)

        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["get_vitals_stats"].invoke({"metric_type": "heart_rate", "days": 7})
        assert dipanggil, "tool tidak memanggil layanan statistik"

    def test_activity_tool_uses_activity_service(
        self, session_factory, db_session, keluarga_dengan_data, monkeypatch
    ) -> None:
        dipanggil = []
        import app.chat.tools as chat_tools

        asli = chat_tools.activity_service.create_activity

        def catat(*args, **kwargs):
            dipanggil.append(True)
            return asli(*args, **kwargs)

        monkeypatch.setattr(chat_tools.activity_service, "create_activity", catat)

        tools = tools_for(session_factory, db_session, keluarga_dengan_data["ayah"]["id"])
        tools["log_activity"].invoke({"category": "coffee"})
        assert dipanggil, "tool tidak memanggil layanan aktivitas"
