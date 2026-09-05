"""Task 5: endpoint register, login, dan profil user.

Acceptance criteria under test:
- Register membuat user, tolak email duplikat, kembalikan token
- Login memvalidasi kredensial; password salah -> 401 tanpa membocorkan keberadaan user
- PATCH /users/me update profil & data fisik dengan validasi rentang
- password_hash tidak pernah muncul di response mana pun
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import User


REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/users/me"


def valid_payload(**overrides) -> dict:
    return {
        "email": "baru@example.com",
        "password": "rahasia-yang-kuat-123",
        "full_name": "Orang Baru",
        **overrides,
    }


# --- Register --------------------------------------------------------------


class TestRegister:
    def test_creates_user_and_returns_token(self, client, db_session) -> None:
        response = client.post(REGISTER, json=valid_payload())
        assert response.status_code == 201, response.text
        assert response.json()["access_token"]
        assert response.json()["token_type"] == "bearer"

        user = db_session.execute(
            select(User).where(User.email == "baru@example.com")
        ).scalar_one()
        assert user.full_name == "Orang Baru"

    def test_password_is_hashed_not_stored_plainly(self, client, db_session) -> None:
        client.post(REGISTER, json=valid_payload())
        user = db_session.execute(select(User)).scalar_one()
        assert user.password_hash != "rahasia-yang-kuat-123"
        assert "rahasia" not in user.password_hash

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

    def test_new_user_is_not_dependent(self, client, db_session) -> None:
        """Register mandiri selalu menghasilkan akun penuh, bukan dependent."""
        client.post(REGISTER, json=valid_payload())
        user = db_session.execute(select(User)).scalar_one()
        assert user.is_dependent is False
        assert user.managed_by_user_id is None


# --- Login -----------------------------------------------------------------


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

    def test_token_from_login_works_on_protected_route(
        self, client, registered_user
    ) -> None:
        token = client.post(
            LOGIN,
            data={
                "username": registered_user["email"],
                "password": registered_user["password"],
            },
        ).json()["access_token"]
        response = client.get(ME, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


# --- Profil ----------------------------------------------------------------


class TestGetProfile:
    def test_returns_own_profile(self, client, auth_headers, registered_user) -> None:
        response = client.get(ME, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["email"] == registered_user["email"]

    def test_requires_authentication(self, client) -> None:
        assert client.get(ME).status_code == 401

    def test_invalid_token_rejected(self, client) -> None:
        response = client.get(ME, headers={"Authorization": "Bearer token-palsu"})
        assert response.status_code == 401


class TestUpdateProfile:
    def test_updates_name(self, client, auth_headers) -> None:
        response = client.patch(
            ME, json={"full_name": "Nama Baru"}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Nama Baru"

    def test_updates_physical_profile(self, client, auth_headers) -> None:
        """Tinggi & berat dipakai chatbot sebagai konteks analisis (FR-4.1)."""
        response = client.patch(
            ME, json={"height_cm": 170.5, "weight": 65.2}, headers=auth_headers
        )
        assert response.status_code == 200
        assert float(response.json()["height_cm"]) == 170.5
        assert float(response.json()["weight"]) == 65.2

    def test_partial_update_keeps_other_fields(
        self, client, auth_headers, registered_user
    ) -> None:
        client.patch(ME, json={"height_cm": 170}, headers=auth_headers)
        response = client.patch(ME, json={"weight": 65}, headers=auth_headers)
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
        ids=["tinggi negatif", "tinggi mustahil", "berat nol", "berat mustahil", "nama kosong"],
    )
    def test_out_of_range_values_rejected(self, client, auth_headers, payload) -> None:
        """Nilai mustahil akan meracuni analisis chatbot (mis. BMI ngawur)."""
        response = client.patch(ME, json=payload, headers=auth_headers)
        assert response.status_code == 422

    def test_cannot_escalate_to_dependent_manager(
        self, client, auth_headers, db_session
    ) -> None:
        """Field yang tidak boleh diubah user harus diabaikan, bukan diterapkan."""
        client.patch(
            ME,
            json={"is_dependent": True, "is_active": False, "email": "lain@example.com"},
            headers=auth_headers,
        )
        user = db_session.execute(select(User)).scalar_one()
        assert user.is_dependent is False
        assert user.is_active is True
        assert user.email == "budi@example.com"

    def test_requires_authentication(self, client) -> None:
        assert client.patch(ME, json={"full_name": "X"}).status_code == 401


# --- Kebocoran data --------------------------------------------------------


class TestNoCredentialLeak:
    """password_hash tidak boleh muncul di response mana pun."""

    def test_register_response_has_no_hash(self, client) -> None:
        body = client.post(REGISTER, json=valid_payload()).text
        assert "password" not in body.lower() or "password_hash" not in body

    def test_profile_response_has_no_hash(self, client, auth_headers) -> None:
        body = client.get(ME, headers=auth_headers).json()
        assert "password_hash" not in body
        assert "password" not in body

    def test_update_response_has_no_hash(self, client, auth_headers) -> None:
        body = client.patch(ME, json={"full_name": "X"}, headers=auth_headers).json()
        assert "password_hash" not in body

    def test_openapi_schema_has_no_hash_field(self, client) -> None:
        """Skema publik pun tidak boleh menyebut password_hash."""
        schemas = client.get("/openapi.json").json()["components"]["schemas"]
        for name, schema in schemas.items():
            assert "password_hash" not in schema.get("properties", {}), (
                f"skema {name} membocorkan password_hash"
            )
