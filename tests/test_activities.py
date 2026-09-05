"""Task 14: endpoint pencatatan aktivitas (PRD FR-7.1, FR-7.3).

Acceptance criteria under test:
- Create memvalidasi kategori dan default occurred_at ke sekarang, bisa ditimpa
- Listing memfilter kategori & rentang tanggal, menghormati accessible_profile_ids
- Admin bisa mencatat atas nama dependent (logged_by_family_member_id != user_id)
- Ubah/hapus terbatas pada subjek atau admin pengelolanya
- Lapisan service bisa dipanggil langsung, supaya tool chatbot (Task 18)
  memakai ulang fungsi yang sama
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import ActivityLog, FamilyMember
from app.services.activity import create_activity, list_activities


ACTIVITIES = "/api/v1/activities"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def payload(**overrides) -> dict:
    return {
        "category": "coffee",
        "quantity": 2,
        "unit": "cups",
        "note": "kopi pagi",
        **overrides,
    }


# --- Membuat ---------------------------------------------------------------


class TestCreate:
    def test_creates_activity(self, client, keluarga) -> None:
        response = client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["category"] == "coffee"
        assert body["quantity"] == 2

    def test_source_is_menu_by_default(self, client, keluarga) -> None:
        """Entri lewat REST berarti dari tombol quick-menu; chatbot nanti
        menandainya `chat` lewat lapisan service yang sama."""
        body = client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["source"] == "menu"

    def test_occurred_at_defaults_to_now(self, client, keluarga, now) -> None:
        body = client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        ).json()
        occurred = datetime.fromisoformat(body["occurred_at"])
        assert abs((occurred - now).total_seconds()) < 60

    def test_occurred_at_can_be_backdated(self, client, keluarga, now) -> None:
        """FamilyMember sering mencatat setelah kejadian — waktunya harus bisa
        dikoreksi (FR-7.1)."""
        kemarin = now - timedelta(days=1)
        body = client.post(
            ACTIVITIES,
            json=payload(occurred_at=kemarin.isoformat()),
            headers=keluarga["ayah"]["headers"],
        ).json()
        assert datetime.fromisoformat(body["occurred_at"]) == kemarin

    @pytest.mark.parametrize(
        "category",
        ["coffee", "exercise", "smoking", "alcohol", "sleep", "meal", "other"],
    )
    def test_all_erd_categories_accepted(self, client, keluarga, category) -> None:
        response = client.post(
            ACTIVITIES,
            json=payload(category=category),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 201

    def test_unknown_category_rejected(self, client, keluarga) -> None:
        response = client.post(
            ACTIVITIES,
            json=payload(category="belanja"),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 422

    def test_optional_fields_may_be_omitted(self, client, keluarga) -> None:
        response = client.post(
            ACTIVITIES, json={"category": "sleep"}, headers=keluarga["ayah"]["headers"]
        )
        assert response.status_code == 201
        assert response.json()["quantity"] is None

    def test_negative_quantity_rejected(self, client, keluarga) -> None:
        response = client.post(
            ACTIVITIES,
            json=payload(quantity=-3),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client) -> None:
        assert client.post(ACTIVITIES, json=payload()).status_code == 401


class TestTimestampFormat:
    """Kontrak API menjanjikan ISO 8601 UTC. Postgres membawa zona waktu
    sendiri, SQLite tidak — tanpa penyeragaman, response dari SQLite
    kehilangan penandanya dan frontend salah menampilkan jam."""

    def test_occurred_at_carries_timezone(self, client, keluarga) -> None:
        body = client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        ).json()
        assert datetime.fromisoformat(body["occurred_at"]).tzinfo is not None

    def test_other_endpoints_carry_timezone(self, client, keluarga) -> None:
        """Diperiksa lintas endpoint, karena masalahnya ada di lapisan
        serialisasi, bukan pada satu endpoint saja."""
        profil = client.get(
            "/api/v1/profiles/me", headers=keluarga["ayah"]["headers"]
        ).json()
        assert datetime.fromisoformat(profil["created_at"]).tzinfo is not None

        daftar = client.get(
            "/api/v1/profiles", headers=keluarga["ayah"]["headers"]
        ).json()
        dibuat = daftar["profiles"][0]["created_at"]
        assert datetime.fromisoformat(dibuat).tzinfo is not None


class TestCreateOnBehalf:
    def test_can_log_for_profile_in_same_account(
        self, client, keluarga, db_session
    ) -> None:
        """Pola subjek vs pelaku: orang tua mencatat untuk anaknya."""
        response = client.post(
            ACTIVITIES,
            json=payload(family_member_id=str(keluarga["anak"]["id"])),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 201, response.text

        row = db_session.execute(select(ActivityLog)).scalar_one()
        assert row.family_member_id == keluarga["anak"]["id"]
        assert row.logged_by_family_member_id == keluarga["ayah"]["id"]

    def test_member_can_also_log_for_sibling(self, client, keluarga) -> None:
        """Satu keluarga satu akun: mencatat "ibu sudah minum obat" adalah
        alur yang wajar, tidak terbatas pada admin."""
        response = client.post(
            ACTIVITIES,
            json=payload(family_member_id=str(keluarga["ibu"]["id"])),
            headers=keluarga["anak"]["headers"],
        )
        assert response.status_code == 201, response.text

    def test_cannot_log_for_other_account(self, client, keluarga) -> None:
        """Batasnya akun: menulis data kesehatan ke akun lain harus ditolak."""
        response = client.post(
            ACTIVITIES,
            json=payload(family_member_id=str(keluarga["luar"]["id"])),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 403

    def test_cannot_log_for_nonexistent_profile(self, client, keluarga) -> None:
        response = client.post(
            ACTIVITIES,
            json=payload(family_member_id=str(uuid.uuid4())),
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code in (403, 404)


# --- Menampilkan -----------------------------------------------------------


class TestList:
    @pytest.fixture
    def isi(self, client, keluarga, now):
        for i, kategori in enumerate(["coffee", "exercise", "coffee", "sleep"]):
            client.post(
                ACTIVITIES,
                json=payload(
                    category=kategori,
                    occurred_at=(now - timedelta(days=i)).isoformat(),
                ),
                headers=keluarga["ayah"]["headers"],
            )
        return keluarga

    def test_lists_own_activities(self, client, isi) -> None:
        body = client.get(ACTIVITIES, headers=isi["ayah"]["headers"]).json()
        assert body["total"] == 4

    def test_filters_by_category(self, client, isi) -> None:
        body = client.get(
            f"{ACTIVITIES}?category=coffee", headers=isi["ayah"]["headers"]
        ).json()
        assert body["total"] == 2

    def test_filters_by_date_range(self, client, isi, now) -> None:
        from urllib.parse import quote

        start = quote((now - timedelta(days=1, hours=1)).isoformat())
        body = client.get(
            f"{ACTIVITIES}?start={start}", headers=isi["ayah"]["headers"]
        ).json()
        assert body["total"] == 2

    def test_newest_first(self, client, isi) -> None:
        activities = client.get(ACTIVITIES, headers=isi["ayah"]["headers"]).json()[
            "activities"
        ]
        stamps = [a["occurred_at"] for a in activities]
        assert stamps == sorted(stamps, reverse=True)

    def test_pagination(self, client, isi) -> None:
        body = client.get(
            f"{ACTIVITIES}?limit=2", headers=isi["ayah"]["headers"]
        ).json()
        assert len(body["activities"]) == 2
        assert body["total"] == 4

    def test_family_member_visible_by_default(self, client, isi) -> None:
        body = client.get(
            f"{ACTIVITIES}?user_id={isi['ayah']['id']}",
            headers=isi["ibu"]["headers"],
        ).json()
        assert body["total"] == 4

    def test_private_activities_hidden(self, client, isi) -> None:
        """Ibu anggota biasa, jadi setelan privat anak benar-benar menutup."""
        client.put(
            "/api/v1/settings/visibility",
            json={"data_type": "activities", "visibility": "private"},
            headers=isi["anak"]["headers"],
        )
        response = client.get(
            f"{ACTIVITIES}?family_member_id={isi['anak']['id']}",
            headers=isi["ibu"]["headers"],
        )
        assert response.status_code == 403

    def test_outsider_forbidden(self, client, isi) -> None:
        response = client.get(
            f"{ACTIVITIES}?family_member_id={isi['ayah']['id']}",
            headers=isi["luar"]["headers"],
        )
        assert response.status_code == 403

    def test_outsider_sees_own_empty_list(self, client, isi) -> None:
        body = client.get(ACTIVITIES, headers=isi["luar"]["headers"]).json()
        assert body["total"] == 0


# --- Mengubah & menghapus --------------------------------------------------


class TestUpdate:
    @pytest.fixture
    def activity(self, client, keluarga):
        return client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        ).json()

    def test_owner_can_update(self, client, keluarga, activity) -> None:
        response = client.patch(
            f"{ACTIVITIES}/{activity['id']}",
            json={"quantity": 5},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 200
        assert response.json()["quantity"] == 5

    def test_can_correct_time(self, client, keluarga, activity, now) -> None:
        koreksi = now - timedelta(hours=3)
        response = client.patch(
            f"{ACTIVITIES}/{activity['id']}",
            json={"occurred_at": koreksi.isoformat()},
            headers=keluarga["ayah"]["headers"],
        )
        assert datetime.fromisoformat(response.json()["occurred_at"]) == koreksi

    def test_family_member_cannot_update(self, client, keluarga, activity) -> None:
        """Boleh melihat bukan berarti boleh mengubah."""
        response = client.patch(
            f"{ACTIVITIES}/{activity['id']}",
            json={"quantity": 99},
            headers=keluarga["ibu"]["headers"],
        )
        assert response.status_code == 403

    def test_admin_can_update_dependent_activity(self, client, keluarga) -> None:
        milik_anak = client.post(
            ACTIVITIES,
            json=payload(user_id=str(keluarga["anak"]["id"])),
            headers=keluarga["ayah"]["headers"],
        ).json()
        response = client.patch(
            f"{ACTIVITIES}/{milik_anak['id']}",
            json={"quantity": 1},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 200

    def test_unknown_activity_404(self, client, keluarga) -> None:
        response = client.patch(
            f"{ACTIVITIES}/{uuid.uuid4()}",
            json={"quantity": 1},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 404


class TestDelete:
    @pytest.fixture
    def activity(self, client, keluarga):
        return client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        ).json()

    def test_owner_can_delete(self, client, keluarga, activity, db_session) -> None:
        response = client.delete(
            f"{ACTIVITIES}/{activity['id']}", headers=keluarga["ayah"]["headers"]
        )
        assert response.status_code == 204
        assert db_session.execute(select(ActivityLog)).first() is None

    def test_family_member_cannot_delete(self, client, keluarga, activity) -> None:
        response = client.delete(
            f"{ACTIVITIES}/{activity['id']}", headers=keluarga["ibu"]["headers"]
        )
        assert response.status_code == 403

    def test_outsider_cannot_delete(self, client, keluarga, activity) -> None:
        response = client.delete(
            f"{ACTIVITIES}/{activity['id']}", headers=keluarga["luar"]["headers"]
        )
        assert response.status_code in (403, 404)


# --- Lapisan service -------------------------------------------------------


class TestServiceLayerReuse:
    """Tool chatbot (Task 18) memanggil fungsi ini langsung, bukan lewat
    HTTP — jadi aturan yang sama berlaku di chat maupun REST."""

    def test_create_callable_directly(self, db_session, keluarga) -> None:
        ayah = db_session.get(FamilyMember, keluarga["ayah"]["id"])
        activity = create_activity(
            db_session,
            actor=ayah,
            subject_id=None,
            category="coffee",
            quantity=2,
            unit="cups",
            note=None,
            occurred_at=None,
            source="chat",
        )
        db_session.commit()
        assert activity.source == "chat"

    def test_list_callable_directly(self, db_session, keluarga, client) -> None:
        client.post(
            ACTIVITIES, json=payload(), headers=keluarga["ayah"]["headers"]
        )
        rows, total = list_activities(
            db_session, viewer_id=keluarga["ayah"]["id"], subject_id=None
        )
        assert total == 1
        assert len(rows) == 1

    def test_service_enforces_same_permission(self, db_session, keluarga) -> None:
        """Aturan izin ada di service, bukan di endpoint — kalau tidak,
        chatbot bisa melewatinya."""
        from app.services.activity import NotAuthorisedToLog

        ayah = db_session.get(FamilyMember, keluarga["ayah"]["id"])
        with pytest.raises(NotAuthorisedToLog):
            create_activity(
                db_session,
                actor=ayah,
                # Profil di akun lain: batas yang harus ditegakkan service,
                # bukan endpoint.
                subject_id=keluarga["luar"]["id"],
                category="coffee",
                quantity=None,
                unit=None,
                note=None,
                occurred_at=None,
                source="chat",
            )
