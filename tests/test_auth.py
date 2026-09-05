"""Endpoint register, login, pilih profil, dan profil aktif.

Acceptance criteria under test:
- Register membuat akun + profil admin sekaligus, tolak email duplikat,
  kembalikan token yang sudah menunjuk profil admin itu
- Login memvalidasi kredensial; password salah -> 401 tanpa membocorkan
  keberadaan akun; token dari login belum menunjuk profil
- PATCH /profiles/{id} update data fisik dengan validasi rentang
- password_hash dan pin_hash tidak pernah muncul di response mana pun
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Account, FamilyMember


REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
SELECT_PROFILE = "/api/v1/auth/select-profile"
ME = "/api/v1/profiles/me"
ACCOUNT_ME = "/api/v1/account/me"


def valid_payload(**overrides) -> dict:
    return {
        "email": "baru@example.com",
        "password": "rahasia-yang-kuat-123",
        "full_name": "Orang Baru",
        **overrides,
    }


# --- Register ----------------------------------------------------------------


class TestRegister:
    def test_creates_account_and_admin_profile(self, client, db_session) -> None:
        response = client.post(REGISTER, json=valid_payload())
        assert response.status_code == 201, response.text
        assert response.json()["access_token"]
        assert response.json()["token_type"] == "bearer"

        account = db_session.execute(
            select(Account).where(Account.email == "baru@example.com")
        ).scalar_one()
        profile = db_session.execute(
            select(FamilyMember).where(FamilyMember.account_id == account.id)
        ).scalar_one()
        assert profile.full_name == "Orang Baru"
        assert profile.role == "admin"

    def test_token_already_selects_admin_profile(self, client) -> None:
        """Pendaftar baru saja membuktikan dirinya; tidak perlu langkah
        pilih-profil tambahan."""
        token = client.post(REGISTER, json=valid_payload()).json()["access_token"]
        response = client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["full_name"] == "Orang Baru"

    def test_password_is_hashed_not_stored_plainly(self, client, db_session) -> None:
        client.post(REGISTER, json=valid_payload())
        account = db_session.execute(select(Account)).scalar_one()
        assert account.password_hash != "rahasia-yang-kuat-123"
        assert "rahasia" not in account.password_hash

    def test_duplicate_email_rejected(self, client) -> None:
        client.post(REGISTER, json=valid_payload())
        response = client.post(REGISTER, json=valid_payload())
        assert response.status_code == 409

    def test_duplicate_email_is_case_insensitive(self, client) -> None:
        """Email hanya beda huruf besar-kecil adalah orang yang sama —
        kalau lolos, dua akun bisa berebut identitas yang sama."""
        client.post(REGISTER, json=valid_payload(email="budi@example.com"))
        response = client.post(REGISTER, json=valid_payload(email="BUDI@example.com"))
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "payload",
        [
            valid_payload(email="bukan-email"),
            valid_payload(email=""),
            valid_payload(full_name=""),
            valid_payload(password="pendek"),
        ],
        ids=["email tidak valid", "email kosong", "nama kosong", "password pendek"],
    )
    def test_invalid_input_rejected(self, client, payload) -> None:
        assert client.post(REGISTER, json=payload).status_code == 422


# --- Login ---------------------------------------------------------------------


class TestLogin:
    def test_valid_credentials_return_token(self, client, registered_user) -> None:
        response = client.post(
            LOGIN,
            data={
                "username": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    def test_wrong_password_rejected(self, client, registered_user) -> None:
        response = client.post(
            LOGIN,
            data={"username": registered_user["email"], "password": "salah-total"},
        )
        assert response.status_code == 401

    def test_unknown_email_rejected(self, client) -> None:
        response = client.post(
            LOGIN, data={"username": "hantu@example.com", "password": "apa pun"}
        )
        assert response.status_code == 401

    def test_error_does_not_reveal_which_field_wrong(
        self, client, registered_user
    ) -> None:
        """Pesan berbeda antara 'email tidak ada' dan 'password salah'
        membocorkan siapa saja yang punya akun."""
        wrong_password = client.post(
            LOGIN,
            data={"username": registered_user["email"], "password": "salah"},
        )
        unknown_email = client.post(
            LOGIN, data={"username": "hantu@example.com", "password": "salah"}
        )
        assert wrong_password.json() == unknown_email.json()

    def test_login_token_has_no_profile_yet(
        self, client, registered_user
    ) -> None:
        """Token dari login butuh langkah pilih-profil sebelum bisa membaca
        data kesehatan — beda dari token hasil register."""
        token = client.post(
            LOGIN,
            data={
                "username": registered_user["email"],
                "password": registered_user["password"],
            },
        ).json()["access_token"]
        response = client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_token_works_on_account_route(self, client, registered_user) -> None:
        """Rute tingkat akun tidak butuh profil dipilih."""
        token = client.post(
            LOGIN,
            data={
                "username": registered_user["email"],
                "password": registered_user["password"],
            },
        ).json()["access_token"]
        response = client.get(
            ACCOUNT_ME, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200


# --- Pilih profil ----------------------------------------------------------


class TestSelectProfile:
    def test_select_admin_profile(
        self, client, registered_user, admin_profile_id
    ) -> None:
        token = client.post(
            LOGIN,
            data={
                "username": registered_user["email"],
                "password": registered_user["password"],
            },
        ).json()["access_token"]

        response = client.post(
            SELECT_PROFILE,
            json={"profile_id": admin_profile_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text

        with_profile = response.json()["access_token"]
        me = client.get(ME, headers={"Authorization": f"Bearer {with_profile}"})
        assert me.status_code == 200

    def test_unknown_profile_rejected(self, client, auth_headers) -> None:
        import uuid

        response = client.post(
            SELECT_PROFILE,
            json={"profile_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_requires_authentication(self, client, admin_profile_id) -> None:
        response = client.post(
            SELECT_PROFILE, json={"profile_id": admin_profile_id}
        )
        assert response.status_code == 401


# --- Profil aktif ------------------------------------------------------------


class TestGetProfile:
    def test_returns_own_profile(self, client, auth_headers) -> None:
        response = client.get(ME, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["full_name"] == "Budi Santoso"

    def test_requires_authentication(self, client) -> None:
        assert client.get(ME).status_code == 401

    def test_invalid_token_rejected(self, client) -> None:
        response = client.get(ME, headers={"Authorization": "Bearer token-palsu"})
        assert response.status_code == 401


class TestUpdateProfile:
    def test_updates_name(self, client, auth_headers, admin_profile_id) -> None:
        response = client.patch(
            f"/api/v1/profiles/{admin_profile_id}",
            json={"full_name": "Nama Baru"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Nama Baru"

    def test_updates_physical_profile(
        self, client, auth_headers, admin_profile_id
    ) -> None:
        """Tinggi & berat dipakai chatbot sebagai konteks analisis (FR-4.1)."""
        response = client.patch(
            f"/api/v1/profiles/{admin_profile_id}",
            json={"height_cm": 170.5, "weight": 65.2},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert float(response.json()["height_cm"]) == 170.5
        assert float(response.json()["weight"]) == 65.2

    def test_partial_update_keeps_other_fields(
        self, client, auth_headers, admin_profile_id
    ) -> None:
        url = f"/api/v1/profiles/{admin_profile_id}"
        client.patch(url, json={"height_cm": 170}, headers=auth_headers)
        response = client.patch(url, json={"weight": 65}, headers=auth_headers)
        assert float(response.json()["height_cm"]) == 170

    @pytest.mark.parametrize(
        "payload",
        [
            {"height_cm": -5},
            {"height_cm": 400},
            {"weight": 0},
            {"weight": 700},
            {"full_name": ""},
        ],
        ids=[
            "tinggi negatif",
            "tinggi mustahil",
            "berat nol",
            "berat mustahil",
            "nama kosong",
        ],
    )
    def test_out_of_range_values_rejected(
        self, client, auth_headers, admin_profile_id, payload
    ) -> None:
        """Nilai mustahil akan meracuni analisis chatbot (mis. BMI ngawur)."""
        response = client.patch(
            f"/api/v1/profiles/{admin_profile_id}", json=payload, headers=auth_headers
        )
        assert response.status_code == 422

    def test_cannot_escalate_role_via_update(
        self, client, auth_headers, admin_profile_id, db_session
    ) -> None:
        """Field yang tidak boleh diubah lewat endpoint ini harus diabaikan,
        bukan diterapkan."""
        client.patch(
            f"/api/v1/profiles/{admin_profile_id}",
            json={"role": "member", "is_active": False},
            headers=auth_headers,
        )
        import uuid

        profile = db_session.get(FamilyMember, uuid.UUID(admin_profile_id))
        assert profile.role == "admin"
        assert profile.is_active is True

    def test_requires_authentication(self, client, admin_profile_id) -> None:
        response = client.patch(
            f"/api/v1/profiles/{admin_profile_id}", json={"full_name": "X"}
        )
        assert response.status_code == 401


# --- Kebocoran data ----------------------------------------------------------


class TestNoCredentialLeak:
    """password_hash dan pin_hash tidak boleh muncul di response mana pun."""

    def test_register_response_has_no_hash(self, client) -> None:
        body = client.post(REGISTER, json=valid_payload()).text
        assert "password_hash" not in body

    def test_profile_response_has_no_hash(self, client, auth_headers) -> None:
        body = client.get(ME, headers=auth_headers).json()
        assert "password_hash" not in body
        assert "pin_hash" not in body

    def test_account_response_has_no_hash(self, client, auth_headers) -> None:
        body = client.get(ACCOUNT_ME, headers=auth_headers).json()
        assert "password_hash" not in body

    def test_update_response_has_no_hash(
        self, client, auth_headers, admin_profile_id
    ) -> None:
        body = client.patch(
            f"/api/v1/profiles/{admin_profile_id}",
            json={"full_name": "X"},
            headers=auth_headers,
        ).json()
        assert "password_hash" not in body
        assert "pin_hash" not in body

    def test_openapi_schema_has_no_hash_field(self, client) -> None:
        """Skema publik pun tidak boleh menyebut password_hash atau pin_hash."""
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for name, schema in schemas.items():
            properties = schema.get("properties", {})
            assert "password_hash" not in properties, (
                f"skema {name} membocorkan password_hash"
            )
            assert "pin_hash" not in properties, f"skema {name} membocorkan pin_hash"
