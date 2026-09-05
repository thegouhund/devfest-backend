"""Visibility service dan pengaturan privasi.

Ini batas keamanan aplikasi: `accessible_profile_ids` menentukan siapa boleh
melihat data kesehatan siapa. Satu celah di sini = kebocoran data medis
antar anggota keluarga, jadi diuji lebih menyeluruh dari bagian lain.

Yang diuji:
- accessible_profile_ids = diri sendiri + profil seakun yang visibility-nya
  `family`, dan seluruh profil seakun kalau pemanggilnya admin
- Setelan private menyembunyikan dari sesama anggota, tapi tidak dari subjek
  sendiri maupun dari admin akun
- Default `family` kalau belum pernah diatur (PRD FR-6.2)
- Profil dari akun lain tidak pernah terlihat, apa pun setelannya
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models import DataVisibilitySetting, FamilyMember
from app.services.visibility import accessible_profile_ids


VISIBILITY = "/api/v1/settings/visibility"


def set_private(db_session, profile_id: uuid.UUID, data_type: str = "vitals") -> None:
    db_session.add(
        DataVisibilitySetting(
            family_member_id=profile_id, data_type=data_type, visibility="private"
        )
    )
    db_session.commit()


class TestAccessibleProfileIds:
    """Matriks siapa-boleh-lihat-siapa. Setiap baris memeriksa himpunan ID
    persis, bukan sekadar 'ada isinya'."""

    def test_sees_whole_account_by_default(self, db_session, keluarga) -> None:
        """Default `family`: sesama profil seakun saling terlihat (FR-6.2)."""
        visible = accessible_profile_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert visible == {
            keluarga["ayah"]["id"],
            keluarga["ibu"]["id"],
            keluarga["anak"]["id"],
        }

    def test_other_account_sees_only_itself(self, db_session, keluarga) -> None:
        """Batas terluar adalah akun: profil akun lain tidak pernah masuk."""
        visible = accessible_profile_ids(db_session, keluarga["luar"]["id"], "vitals")
        assert visible == {keluarga["luar"]["id"]}

    def test_private_hides_from_siblings(self, db_session, keluarga) -> None:
        set_private(db_session, keluarga["ibu"]["id"])
        visible = accessible_profile_ids(db_session, keluarga["anak"]["id"], "vitals")
        assert keluarga["ibu"]["id"] not in visible

    def test_private_still_visible_to_self(self, db_session, keluarga) -> None:
        """Menandai privat tidak boleh menyembunyikan data dari pemiliknya."""
        set_private(db_session, keluarga["ibu"]["id"])
        visible = accessible_profile_ids(db_session, keluarga["ibu"]["id"], "vitals")
        assert keluarga["ibu"]["id"] in visible

    def test_private_still_visible_to_admin(self, db_session, keluarga) -> None:
        """Admin mengelola seluruh akun, termasuk profil yang menandai privat
        dari dashboard gabungan (FR-6.4)."""
        set_private(db_session, keluarga["ibu"]["id"])
        visible = accessible_profile_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["ibu"]["id"] in visible

    def test_member_does_not_inherit_admin_reach(self, db_session, keluarga) -> None:
        """Anggota biasa tidak boleh melihat profil privat hanya karena
        seakun — kalau ini bocor, setelan privat jadi tidak ada artinya."""
        set_private(db_session, keluarga["anak"]["id"])
        visible = accessible_profile_ids(db_session, keluarga["ibu"]["id"], "vitals")
        assert keluarga["anak"]["id"] not in visible

    def test_setting_all_covers_every_data_type(self, db_session, keluarga) -> None:
        set_private(db_session, keluarga["ibu"]["id"], data_type="all")
        for data_type in ("vitals", "activities"):
            visible = accessible_profile_ids(
                db_session, keluarga["anak"]["id"], data_type
            )
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
        vitals = accessible_profile_ids(db_session, keluarga["anak"]["id"], "vitals")
        activities = accessible_profile_ids(
            db_session, keluarga["anak"]["id"], "activities"
        )
        assert keluarga["ibu"]["id"] in vitals
        assert keluarga["ibu"]["id"] not in activities

    def test_private_setting_does_not_leak_to_other_type(
        self, db_session, keluarga
    ) -> None:
        set_private(db_session, keluarga["ibu"]["id"], data_type="vitals")
        activities = accessible_profile_ids(
            db_session, keluarga["anak"]["id"], "activities"
        )
        assert keluarga["ibu"]["id"] in activities

    def test_deactivated_profile_excluded(self, db_session, keluarga) -> None:
        """Profil nonaktif tidak lagi muncul di daftar keluarga."""
        ibu = db_session.get(FamilyMember, keluarga["ibu"]["id"])
        ibu.is_active = False
        db_session.commit()
        visible = accessible_profile_ids(db_session, keluarga["ayah"]["id"], "vitals")
        assert keluarga["ibu"]["id"] not in visible

    def test_unknown_data_type_falls_back_to_default(
        self, db_session, keluarga
    ) -> None:
        """Jenis data tak dikenal tidak boleh crash atau membuka akses lebih
        luas dari default."""
        visible = accessible_profile_ids(
            db_session, keluarga["ayah"]["id"], "belum_ada"
        )
        assert keluarga["ayah"]["id"] in visible
        assert keluarga["luar"]["id"] not in visible

    def test_unknown_profile_sees_nothing(self, db_session) -> None:
        assert accessible_profile_ids(db_session, uuid.uuid4(), "vitals") == set()

    def test_private_does_not_cross_accounts(self, db_session, keluarga) -> None:
        """Bahkan admin akun lain tidak boleh melihat profil di akun ini."""
        visible = accessible_profile_ids(db_session, keluarga["luar"]["id"], "vitals")
        assert keluarga["ayah"]["id"] not in visible
        assert keluarga["ibu"]["id"] not in visible


class TestVisibilitySettingsEndpoint:
    def test_default_is_family(self, client, keluarga) -> None:
        response = client.get(VISIBILITY, headers=keluarga["ibu"]["headers"])
        assert response.status_code == 200
        settings = {s["data_type"]: s["visibility"] for s in response.json()["settings"]}
        assert set(settings.values()) == {"family"}

    def test_update_is_upsert(self, client, keluarga) -> None:
        """Menyetel dua kali tidak boleh menabrak UNIQUE(profil, data_type)."""
        for visibility in ("private", "family"):
            response = client.put(
                VISIBILITY,
                json={"data_type": "vitals", "visibility": visibility},
                headers=keluarga["ibu"]["headers"],
            )
            assert response.status_code == 200, response.text

        settings = {
            s["data_type"]: s["visibility"]
            for s in client.get(VISIBILITY, headers=keluarga["ibu"]["headers"]).json()[
                "settings"
            ]
        }
        assert settings["vitals"] == "family"

    def test_setting_applies_to_own_profile_only(self, client, keluarga) -> None:
        """Profil diambil dari token, tidak pernah dari body — kalau tidak,
        siapa pun bisa mengubah setelan privasi orang lain."""
        client.put(
            VISIBILITY,
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ibu"]["headers"],
        )
        anak = client.get(VISIBILITY, headers=keluarga["anak"]["headers"]).json()
        settings = {s["data_type"]: s["visibility"] for s in anak["settings"]}
        assert settings["vitals"] == "family"

    def test_requires_authentication(self, client) -> None:
        assert client.get(VISIBILITY).status_code == 401

    @pytest.mark.parametrize(
        "payload",
        [
            {"data_type": "vitals", "visibility": "semua_orang"},
            {"data_type": "apa_saja", "visibility": "private"},
        ],
        ids=["visibility tak dikenal", "data_type tak dikenal"],
    )
    def test_invalid_payload_rejected(self, client, keluarga, payload) -> None:
        response = client.put(
            VISIBILITY, json=payload, headers=keluarga["ibu"]["headers"]
        )
        assert response.status_code == 422
