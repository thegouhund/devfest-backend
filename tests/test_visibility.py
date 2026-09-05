"""Task 7: profil dependent, visibility service, pengaturan privasi.

Ini batas keamanan aplikasi: `accessible_user_ids` menentukan siapa boleh
melihat data kesehatan siapa. Satu celah di sini = kebocoran data medis
antar anggota keluarga, jadi diuji lebih menyeluruh dari bagian lain.

Acceptance criteria under test:
- POST dependents membuat is_dependent=true + managed_by_user_id, hanya admin
- accessible_user_ids = diri sendiri + anggota family yang visibility-nya family
  + dependent yang dikelola
- Setelan private menyembunyikan dari sesama anggota, tapi tidak dari subjek
  sendiri maupun admin yang mengelolanya
- Default `family` kalau belum pernah diatur (PRD FR-6.2)
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import DataVisibilitySetting, User
from app.services.visibility import accessible_user_ids


FAMILIES = "/api/v1/families"
VISIBILITY = "/api/v1/settings/visibility"


def register(client, email: str, name: str) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "rahasia-kuat-123", "full_name": name},
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = client.get("/api/v1/users/me", headers=headers).json()["id"]
    return {"headers": headers, "id": uuid.UUID(user_id)}


@pytest.fixture
def keluarga(client, db_session):
    """Satu keluarga: ayah (admin), ibu (member), anak (dependent), plus
    orang luar yang tidak tergabung."""
    ayah = register(client, "ayah@x.com", "Ayah")
    ibu = register(client, "ibu@x.com", "Ibu")
    luar = register(client, "luar@x.com", "Orang Luar")

    family = client.post(
        FAMILIES, json={"name": "Keluarga Uji"}, headers=ayah["headers"]
    ).json()
    client.post(
        f"{FAMILIES}/join",
        json={"invite_code": family["invite_code"]},
        headers=ibu["headers"],
    )

    anak = client.post(
        f"{FAMILIES}/{family['id']}/dependents",
        json={"full_name": "Anak", "date_of_birth": "2015-03-10"},
        headers=ayah["headers"],
    )
    assert anak.status_code == 201, anak.text

    return {
        "family": family,
        "ayah": ayah,
        "ibu": ibu,
        "luar": luar,
        "anak": {"id": uuid.UUID(anak.json()["id"])},
    }


# --- Profil dependent ------------------------------------------------------


class TestCreateDependent:
    def test_creates_dependent_without_credentials(self, client, keluarga, db_session) -> None:
        anak = db_session.get(User, keluarga["anak"]["id"])
        assert anak.is_dependent is True
        assert anak.managed_by_user_id == keluarga["ayah"]["id"]
        assert anak.email is None
        assert anak.password_hash is None

    def test_dependent_appears_in_member_list(self, client, keluarga) -> None:
        members = client.get(
            f"{FAMILIES}/{keluarga['family']['id']}/members",
            headers=keluarga["ayah"]["headers"],
        ).json()["members"]
        dependents = [m for m in members if m["is_dependent"]]
        assert len(dependents) == 1
        assert dependents[0]["full_name"] == "Anak"

    def test_member_cannot_create_dependent(self, client, keluarga) -> None:
        response = client.post(
            f"{FAMILIES}/{keluarga['family']['id']}/dependents",
            json={"full_name": "Anak Lain"},
            headers=keluarga["ibu"]["headers"],
        )
        assert response.status_code == 403

    def test_outsider_cannot_create_dependent(self, client, keluarga) -> None:
        response = client.post(
            f"{FAMILIES}/{keluarga['family']['id']}/dependents",
            json={"full_name": "Penyusup"},
            headers=keluarga["luar"]["headers"],
        )
        assert response.status_code == 403

    def test_dependent_cannot_log_in(self, client, keluarga) -> None:
        """Dependent tanpa password tidak boleh bisa masuk dengan cara apa pun."""
        response = client.post(
            "/api/v1/auth/login", data={"username": "", "password": ""}
        )
        assert response.status_code in (401, 422)

    @pytest.mark.parametrize(
        "payload",
        [{"full_name": ""}, {"full_name": "   "}, {"full_name": "A", "height_cm": 400}],
        ids=["nama kosong", "nama spasi", "tinggi mustahil"],
    )
    def test_invalid_payload_rejected(self, client, keluarga, payload) -> None:
        response = client.post(
            f"{FAMILIES}/{keluarga['family']['id']}/dependents",
            json=payload,
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 422


# --- Visibility service ----------------------------------------------------


class TestAccessibleUserIds:
    """Matriks siapa-boleh-lihat-siapa. Setiap baris memeriksa himpunan ID
    persis, bukan sekadar 'ada isinya'."""

    def test_sees_self_and_family_by_default(self, db_session, keluarga) -> None:
        """Default `family`: sesama anggota saling terlihat (PRD FR-6.2)."""
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert visible == {
            keluarga["ayah"]["id"],
            keluarga["ibu"]["id"],
            keluarga["anak"]["id"],
        }

    def test_outsider_sees_only_self(self, db_session, keluarga) -> None:
        visible = accessible_user_ids(db_session, keluarga["luar"]["id"], "vitals")
        assert visible == {keluarga["luar"]["id"]}

    def test_private_hides_from_siblings(self, db_session, client, keluarga) -> None:
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["ibu"]["id"] not in visible

    def test_private_still_visible_to_self(self, db_session, client, keluarga) -> None:
        """Menandai privat tidak boleh menyembunyikan data dari pemiliknya sendiri."""
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        visible = accessible_user_ids(db_session, keluarga["ibu"]["id"], "vitals")
        assert keluarga["ibu"]["id"] in visible

    def test_private_dependent_still_visible_to_manager(
        self, db_session, client, keluarga
    ) -> None:
        """Admin yang mengelola dependent tetap harus bisa melihat datanya —
        dia yang bertanggung jawab atas kesehatan anak tersebut (FR-6.2)."""
        db_session.add(
            DataVisibilitySetting(
                user_id=keluarga["anak"]["id"],
                data_type="vitals",
                visibility="private",
            )
        )
        db_session.commit()
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["anak"]["id"] in visible

    def test_private_dependent_hidden_from_other_member(
        self, db_session, keluarga
    ) -> None:
        """Ibu bukan pengelola anak, jadi data privat anak tersembunyi darinya."""
        db_session.add(
            DataVisibilitySetting(
                user_id=keluarga["anak"]["id"],
                data_type="vitals",
                visibility="private",
            )
        )
        db_session.commit()
        visible = accessible_user_ids(db_session, keluarga["ibu"]["id"], "vitals")
        assert keluarga["anak"]["id"] not in visible

    def test_setting_all_covers_every_data_type(
        self, db_session, client, keluarga
    ) -> None:
        """`all` harus berlaku untuk vitals maupun activities."""
        client.put(
            VISIBILITY,
            json={"data_type": "all", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        for data_type in ("vitals", "activities"):
            visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], data_type)
            assert keluarga["ibu"]["id"] not in visible, data_type

    def test_specific_setting_wins_over_all(self, db_session, client, keluarga) -> None:
        """Setelan spesifik lebih diutamakan daripada `all` yang lebih umum."""
        client.put(
            VISIBILITY,
            json={"data_type": "all", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "family"},
            headers=keluarga["ibu"]["headers"],
        )
        vitals = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        activities = accessible_user_ids(db_session, keluarga["ayah"]["id"], "activities")
        assert keluarga["ibu"]["id"] in vitals
        assert keluarga["ibu"]["id"] not in activities

    def test_private_setting_does_not_leak_to_other_type(
        self, db_session, client, keluarga
    ) -> None:
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        activities = accessible_user_ids(db_session, keluarga["ayah"]["id"], "activities")
        assert keluarga["ibu"]["id"] in activities

    def test_removed_member_loses_visibility(self, db_session, client, keluarga) -> None:
        """Anggota yang dikeluarkan tidak boleh tetap melihat data keluarga."""
        client.delete(
            f"{FAMILIES}/{keluarga['family']['id']}/members/{keluarga['ibu']['id']}",
            headers=keluarga["ayah"]["headers"],
        )
        db_session.expire_all()
        visible = accessible_user_ids(db_session, keluarga["ibu"]["id"], "vitals")
        assert visible == {keluarga["ibu"]["id"]}

    def test_removed_member_no_longer_visible_to_others(
        self, db_session, client, keluarga
    ) -> None:
        client.delete(
            f"{FAMILIES}/{keluarga['family']['id']}/members/{keluarga['ibu']['id']}",
            headers=keluarga["ayah"]["headers"],
        )
        db_session.expire_all()
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["ibu"]["id"] not in visible

    def test_deactivated_member_still_visible(self, db_session, keluarga) -> None:
        """Keputusan produk: akun nonaktif = tidak bisa login, tapi riwayat
        kesehatannya tetap tampil supaya grafik tren keluarga tidak bolong.
        Berbeda dari keanggotaan `removed`, yang memang mencabut akses."""
        ibu = db_session.get(User, keluarga["ibu"]["id"])
        ibu.is_active = False
        db_session.commit()
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["ibu"]["id"] in visible

    def test_data_type_unknown_falls_back_to_default(self, db_session, keluarga) -> None:
        """Jenis data tak dikenal tidak boleh membuat crash atau membuka akses
        lebih luas dari default."""
        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "belum_ada")
        assert keluarga["ayah"]["id"] in visible
        assert keluarga["luar"]["id"] not in visible

    def test_other_family_never_visible(self, db_session, client, keluarga) -> None:
        """Anggota keluarga lain tidak boleh terlihat sama sekali."""
        lain = register(client, "lain@x.com", "Keluarga Lain")
        keluarga_lain = client.post(
            FAMILIES, json={"name": "Keluarga Lain"}, headers=lain["headers"]
        ).json()
        assert keluarga_lain["id"]

        visible = accessible_user_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert lain["id"] not in visible

    def test_unknown_user_sees_nothing(self, db_session) -> None:
        assert accessible_user_ids(db_session, uuid.uuid4(), "vitals") == set()

    def test_never_returns_empty_for_valid_user(self, db_session, keluarga) -> None:
        """User valid harus selalu bisa melihat dirinya sendiri, apa pun setelannya."""
        for person in ("ayah", "ibu", "luar"):
            visible = accessible_user_ids(db_session, keluarga[person]["id"], "vitals")
            assert keluarga[person]["id"] in visible, person


# --- Endpoint pengaturan privasi -------------------------------------------


class TestVisibilitySettings:
    def test_defaults_to_family_when_never_set(self, client, keluarga) -> None:
        response = client.get(VISIBILITY, headers=keluarga["ibu"]["headers"])
        assert response.status_code == 200
        settings = {s["data_type"]: s["visibility"] for s in response.json()["settings"]}
        assert settings["vitals"] == "family"
        assert settings["activities"] == "family"

    def test_update_is_reflected(self, client, keluarga) -> None:
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        response = client.get(VISIBILITY, headers=keluarga["ibu"]["headers"])
        settings = {s["data_type"]: s["visibility"] for s in response.json()["settings"]}
        assert settings["vitals"] == "private"

    def test_update_twice_does_not_duplicate(self, client, keluarga, db_session) -> None:
        """UNIQUE(user_id, data_type) — update kedua harus menimpa, bukan menambah."""
        for visibility in ("private", "family"):
            client.put(
                VISIBILITY,
                json={"data_type": "vitals", "visibility": visibility},
                headers=keluarga["ibu"]["headers"],
            )
        rows = (
            db_session.execute(
                select(DataVisibilitySetting).where(
                    DataVisibilitySetting.user_id == keluarga["ibu"]["id"],
                    DataVisibilitySetting.data_type == "vitals",
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].visibility == "family"

    @pytest.mark.parametrize(
        "payload",
        [
            {"data_type": "ngawur", "visibility": "family"},
            {"data_type": "vitals", "visibility": "rahasia"},
            {"data_type": "vitals"},
        ],
        ids=["data_type invalid", "visibility invalid", "tanpa visibility"],
    )
    def test_invalid_payload_rejected(self, client, keluarga, payload) -> None:
        response = client.put(
            VISIBILITY, json=payload, headers=keluarga["ibu"]["headers"]
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client) -> None:
        assert client.get(VISIBILITY).status_code == 401
        assert client.put(
            VISIBILITY, json={"data_type": "vitals", "visibility": "private"}
        ).status_code == 401

    def test_setting_only_affects_own_data(self, client, keluarga, db_session) -> None:
        """Setelan privasi milik pemanggil, bukan bisa diterapkan ke orang lain."""
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        rows = (
            db_session.execute(select(DataVisibilitySetting)).scalars().all()
        )
        assert all(r.user_id == keluarga["ibu"]["id"] for r in rows)
