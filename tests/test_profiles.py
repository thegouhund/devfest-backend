"""Endpoint profil: admin membuat profil untuk anggota keluarga.

Model satu-akun-per-keluarga (ERD §0) menghapus undangan dan bergabung.
Yang menggantikannya, dan diuji di sini:
- registrasi membuat akun + profil admin sekaligus
- hanya admin yang boleh menambah/menonaktifkan profil
- profil akun lain tidak pernah terlihat atau bisa disentuh
- PIN mengunci pemilihan profil, dan hash-nya tidak pernah keluar
"""

from __future__ import annotations

import pytest


@pytest.fixture
def other_account(client):
    """Akun kedua, untuk memastikan batas antar-akun benar-benar rapat."""
    payload = {
        "email": "siti@example.com",
        "password": "rahasia-yang-kuat-456",
        "full_name": "Siti Aminah",
    }
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}}


class TestRegistration:
    def test_register_creates_admin_profile(self, client, auth_headers) -> None:
        response = client.get("/api/v1/profiles/me", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["full_name"] == "Budi Santoso"
        assert body["role"] == "admin"

    def test_duplicate_email_rejected(self, client, registered_user) -> None:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": registered_user["email"],
                "password": "password-lain-yang-kuat",
                "full_name": "Orang Lain",
            },
        )
        assert response.status_code == 409


class TestCreateProfile:
    def test_admin_creates_member_profile(self, client, auth_headers) -> None:
        response = client.post(
            "/api/v1/profiles",
            json={"full_name": "Ibu Ani", "relationship_label": "ibu"},
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["full_name"] == "Ibu Ani"
        assert body["role"] == "member"
        assert body["relationship_label"] == "ibu"
        assert body["has_pin"] is False

    def test_member_cannot_create_profile(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        token = _token_for(client, auth_headers, anak["id"])

        response = client.post(
            "/api/v1/profiles",
            json={"full_name": "Selundupan"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    def test_requires_authentication(self, client) -> None:
        response = client.post("/api/v1/profiles", json={"full_name": "X"})
        assert response.status_code == 401

    @pytest.mark.parametrize("nama", ["", "   "])
    def test_blank_name_rejected(self, client, auth_headers, nama: str) -> None:
        response = client.post(
            "/api/v1/profiles", json={"full_name": nama}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_impossible_height_rejected(self, client, auth_headers) -> None:
        """Tinggi di luar batas tubuh manusia meracuni analisis chatbot."""
        response = client.post(
            "/api/v1/profiles",
            json={"full_name": "Raksasa", "height_cm": 900},
            headers=auth_headers,
        )
        assert response.status_code == 422


class TestListProfiles:
    def test_lists_only_own_account(
        self, client, auth_headers, make_profile, other_account
    ) -> None:
        make_profile("Ibu Ani")

        mine = client.get("/api/v1/profiles", headers=auth_headers).json()["profiles"]
        theirs = client.get(
            "/api/v1/profiles", headers=other_account["headers"]
        ).json()["profiles"]

        assert {p["full_name"] for p in mine} == {"Budi Santoso", "Ibu Ani"}
        assert {p["full_name"] for p in theirs} == {"Siti Aminah"}

    def test_pin_hash_never_exposed(self, client, auth_headers, make_profile) -> None:
        make_profile("Kakek", pin="1234")
        body = client.get("/api/v1/profiles", headers=auth_headers).text
        assert "pin_hash" not in body
        assert "1234" not in body


class TestSelectProfile:
    def test_select_profile_without_pin(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        response = client.post(
            "/api/v1/auth/select-profile",
            json={"profile_id": anak["id"]},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_pin_required_when_set(self, client, auth_headers, make_profile) -> None:
        kakek = make_profile("Kakek", pin="1234")

        tanpa_pin = client.post(
            "/api/v1/auth/select-profile",
            json={"profile_id": kakek["id"]},
            headers=auth_headers,
        )
        assert tanpa_pin.status_code == 401

        salah = client.post(
            "/api/v1/auth/select-profile",
            json={"profile_id": kakek["id"], "pin": "9999"},
            headers=auth_headers,
        )
        assert salah.status_code == 401

        benar = client.post(
            "/api/v1/auth/select-profile",
            json={"profile_id": kakek["id"], "pin": "1234"},
            headers=auth_headers,
        )
        assert benar.status_code == 200

    def test_cannot_select_profile_of_other_account(
        self, client, auth_headers, other_account
    ) -> None:
        """Profil akun lain dijawab 404, bukan 403 — 403 sudah membocorkan
        bahwa id itu ada."""
        theirs = client.get(
            "/api/v1/profiles", headers=other_account["headers"]
        ).json()["profiles"][0]

        response = client.post(
            "/api/v1/auth/select-profile",
            json={"profile_id": theirs["id"]},
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestUpdateProfile:
    def test_admin_can_edit_any_profile_in_account(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        response = client.patch(
            f"/api/v1/profiles/{anak['id']}",
            json={"height_cm": 150},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["height_cm"] == 150

    def test_member_cannot_edit_sibling(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        kakak = make_profile("Kakak")
        token = _token_for(client, auth_headers, anak["id"])

        response = client.patch(
            f"/api/v1/profiles/{kakak['id']}",
            json={"height_cm": 150},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    def test_member_can_edit_self(self, client, auth_headers, make_profile) -> None:
        anak = make_profile("Anak")
        token = _token_for(client, auth_headers, anak["id"])

        response = client.patch(
            f"/api/v1/profiles/{anak['id']}",
            json={"height_cm": 150},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200

    def test_cannot_edit_profile_of_other_account(
        self, client, auth_headers, other_account
    ) -> None:
        theirs = client.get(
            "/api/v1/profiles", headers=other_account["headers"]
        ).json()["profiles"][0]

        response = client.patch(
            f"/api/v1/profiles/{theirs['id']}",
            json={"height_cm": 150},
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestDeactivateProfile:
    def test_admin_can_deactivate_member(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        response = client.delete(
            f"/api/v1/profiles/{anak['id']}", headers=auth_headers
        )
        assert response.status_code == 204

        tersisa = client.get("/api/v1/profiles", headers=auth_headers).json()[
            "profiles"
        ]
        assert anak["id"] not in {p["id"] for p in tersisa}

    def test_deactivation_keeps_health_history(
        self, client, auth_headers, make_profile, db_session
    ) -> None:
        """Ditandai nonaktif, bukan dihapus: menghapus baris profil ikut
        menghapus seluruh riwayat kesehatannya lewat cascade."""
        from app.db.models import FamilyMember

        anak = make_profile("Anak")
        client.delete(f"/api/v1/profiles/{anak['id']}", headers=auth_headers)

        row = db_session.get(FamilyMember, __import__("uuid").UUID(anak["id"]))
        assert row is not None
        assert row.is_active is False

    def test_cannot_deactivate_last_admin(
        self, client, auth_headers, admin_profile_id
    ) -> None:
        """Akun tanpa admin tidak punya jalan keluar: tidak ada login
        terpisah per profil yang bisa menaikkan admin baru."""
        response = client.delete(
            f"/api/v1/profiles/{admin_profile_id}", headers=auth_headers
        )
        assert response.status_code == 409

    def test_member_cannot_deactivate(
        self, client, auth_headers, make_profile
    ) -> None:
        anak = make_profile("Anak")
        kakak = make_profile("Kakak")
        token = _token_for(client, auth_headers, anak["id"])

        response = client.delete(
            f"/api/v1/profiles/{kakak['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


def _token_for(client, auth_headers, profile_id: str) -> str:
    """Token yang menunjuk profil tertentu dalam akun yang sedang login."""
    response = client.post(
        "/api/v1/auth/select-profile",
        json={"profile_id": profile_id},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]
