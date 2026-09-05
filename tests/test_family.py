"""Task 6: endpoint family group.

Acceptance criteria under test:
- Create -> baris families + family_members dengan role admin
- Join kode valid -> anggota aktif; kode salah 404; join dua kali 409
- Ubah role & keluarkan anggota hanya boleh admin (selain itu 403)
- Keluarkan anggota menandai status 'removed', bukan menghapus baris
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Family, FamilyMember


FAMILIES = "/api/v1/families"


@pytest.fixture
def other_user(client):
    """User kedua yang belum punya family."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "siti@example.com",
            "password": "rahasia-yang-kuat-123",
            "full_name": "Siti Rahayu",
        },
    )
    assert response.status_code == 201
    token = response.json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "token": token}


@pytest.fixture
def family(client, auth_headers):
    """Family baru dengan `registered_user` sebagai admin."""
    response = client.post(
        FAMILIES, json={"name": "Keluarga Santoso"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def family_with_member(client, family, other_user):
    """Family yang sudah punya satu member biasa."""
    response = client.post(
        f"{FAMILIES}/join",
        json={"invite_code": family["invite_code"]},
        headers=other_user["headers"],
    )
    assert response.status_code == 200, response.text
    return family


def user_id_of(client, headers) -> str:
    return client.get("/api/v1/users/me", headers=headers).json()["id"]


# --- Buat family -----------------------------------------------------------


class TestCreateFamily:
    def test_creates_family_and_makes_creator_admin(
        self, client, auth_headers, db_session
    ) -> None:
        response = client.post(
            FAMILIES, json={"name": "Keluarga Santoso"}, headers=auth_headers
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["name"] == "Keluarga Santoso"
        assert body["invite_code"]

        membership = db_session.execute(select(FamilyMember)).scalar_one()
        assert membership.role == "admin"
        assert membership.status == "active"

    def test_invite_codes_are_unique(self, client, auth_headers, other_user) -> None:
        first = client.post(FAMILIES, json={"name": "A"}, headers=auth_headers).json()
        second = client.post(
            FAMILIES, json={"name": "B"}, headers=other_user["headers"]
        ).json()
        assert first["invite_code"] != second["invite_code"]

    def test_invite_code_is_not_guessable(self, client, auth_headers) -> None:
        """Kode tebakan berarti orang asing bisa masuk ke data kesehatan keluarga."""
        code = client.post(
            FAMILIES, json={"name": "X"}, headers=auth_headers
        ).json()["invite_code"]
        assert len(code) >= 6

    @pytest.mark.parametrize(
        "payload", [{"name": ""}, {"name": "   "}, {}], ids=["kosong", "spasi", "tanpa nama"]
    )
    def test_invalid_name_rejected(self, client, auth_headers, payload) -> None:
        assert client.post(FAMILIES, json=payload, headers=auth_headers).status_code == 422

    def test_requires_authentication(self, client) -> None:
        assert client.post(FAMILIES, json={"name": "X"}).status_code == 401


# --- Gabung family ---------------------------------------------------------


class TestJoinFamily:
    def test_join_with_valid_code(self, client, family, other_user) -> None:
        response = client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family["invite_code"]},
            headers=other_user["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == family["id"]

    def test_joined_user_has_member_role(
        self, client, family, other_user, db_session
    ) -> None:
        client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family["invite_code"]},
            headers=other_user["headers"],
        )
        memberships = db_session.execute(select(FamilyMember)).scalars().all()
        roles = {m.role for m in memberships}
        assert roles == {"admin", "member"}

    def test_invalid_code_rejected(self, client, other_user) -> None:
        response = client.post(
            f"{FAMILIES}/join",
            json={"invite_code": "KODE-NGAWUR"},
            headers=other_user["headers"],
        )
        assert response.status_code == 404

    def test_joining_twice_rejected(self, client, family, other_user) -> None:
        client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family["invite_code"]},
            headers=other_user["headers"],
        )
        response = client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family["invite_code"]},
            headers=other_user["headers"],
        )
        assert response.status_code == 409

    def test_creator_cannot_join_own_family(self, client, family, auth_headers) -> None:
        response = client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family["invite_code"]},
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_removed_member_can_rejoin(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        """Anggota yang dikeluarkan lalu diundang lagi harus bisa masuk kembali,
        bukan tertahan constraint unik dari keanggotaan lamanya."""
        member_id = user_id_of(client, other_user["headers"])
        client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            headers=auth_headers,
        )
        response = client.post(
            f"{FAMILIES}/join",
            json={"invite_code": family_with_member["invite_code"]},
            headers=other_user["headers"],
        )
        assert response.status_code == 200, response.text


# --- Daftar anggota --------------------------------------------------------


class TestListMembers:
    def test_lists_members_with_roles(self, client, family_with_member, auth_headers) -> None:
        response = client.get(
            f"{FAMILIES}/{family_with_member['id']}/members", headers=auth_headers
        )
        assert response.status_code == 200
        members = response.json()["members"]
        assert len(members) == 2
        assert {m["role"] for m in members} == {"admin", "member"}
        assert all("full_name" in m for m in members)

    def test_member_can_also_list(self, client, family_with_member, other_user) -> None:
        response = client.get(
            f"{FAMILIES}/{family_with_member['id']}/members",
            headers=other_user["headers"],
        )
        assert response.status_code == 200

    def test_outsider_cannot_list(self, client, family, other_user) -> None:
        """Daftar anggota keluarga orang lain bukan data publik."""
        response = client.get(
            f"{FAMILIES}/{family['id']}/members", headers=other_user["headers"]
        )
        assert response.status_code == 403

    def test_removed_member_excluded(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        member_id = user_id_of(client, other_user["headers"])
        client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            headers=auth_headers,
        )
        members = client.get(
            f"{FAMILIES}/{family_with_member['id']}/members", headers=auth_headers
        ).json()["members"]
        assert len(members) == 1

    def test_unknown_family_returns_404(self, client, auth_headers) -> None:
        import uuid

        response = client.get(f"{FAMILIES}/{uuid.uuid4()}/members", headers=auth_headers)
        assert response.status_code in (403, 404)


# --- Ubah role -------------------------------------------------------------


class TestUpdateRole:
    def test_admin_can_promote_member(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        member_id = user_id_of(client, other_user["headers"])
        response = client.patch(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            json={"role": "admin"},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["role"] == "admin"

    def test_member_cannot_promote_self(
        self, client, family_with_member, other_user
    ) -> None:
        member_id = user_id_of(client, other_user["headers"])
        response = client.patch(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            json={"role": "admin"},
            headers=other_user["headers"],
        )
        assert response.status_code == 403

    def test_invalid_role_rejected(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        member_id = user_id_of(client, other_user["headers"])
        response = client.patch(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            json={"role": "superadmin"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    def test_cannot_demote_last_admin(
        self, client, family_with_member, auth_headers
    ) -> None:
        """Family tanpa admin tidak bisa dikelola siapa pun lagi — anggota
        tidak bisa diundang, diubah, atau dikeluarkan selamanya."""
        admin_id = user_id_of(client, auth_headers)
        response = client.patch(
            f"{FAMILIES}/{family_with_member['id']}/members/{admin_id}",
            json={"role": "member"},
            headers=auth_headers,
        )
        assert response.status_code == 409


# --- Keluarkan anggota -----------------------------------------------------


class TestRemoveMember:
    def test_admin_can_remove_member(
        self, client, family_with_member, other_user, auth_headers, db_session
    ) -> None:
        member_id = user_id_of(client, other_user["headers"])
        response = client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            headers=auth_headers,
        )
        assert response.status_code == 204

    def test_removal_marks_status_not_deletes_row(
        self, client, family_with_member, other_user, auth_headers, db_session
    ) -> None:
        """Baris keanggotaan disimpan sebagai jejak, bukan dihapus."""
        member_id = user_id_of(client, other_user["headers"])
        client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            headers=auth_headers,
        )
        db_session.expire_all()
        memberships = db_session.execute(select(FamilyMember)).scalars().all()
        assert len(memberships) == 2
        assert {m.status for m in memberships} == {"active", "removed"}

    def test_member_cannot_remove_others(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        admin_id = user_id_of(client, auth_headers)
        response = client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{admin_id}",
            headers=other_user["headers"],
        )
        assert response.status_code == 403

    def test_cannot_remove_last_admin(
        self, client, family_with_member, auth_headers
    ) -> None:
        admin_id = user_id_of(client, auth_headers)
        response = client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{admin_id}",
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_removed_member_loses_access(
        self, client, family_with_member, other_user, auth_headers
    ) -> None:
        """Setelah dikeluarkan, anggota tidak boleh bisa melihat data keluarga lagi."""
        member_id = user_id_of(client, other_user["headers"])
        client.delete(
            f"{FAMILIES}/{family_with_member['id']}/members/{member_id}",
            headers=auth_headers,
        )
        response = client.get(
            f"{FAMILIES}/{family_with_member['id']}/members",
            headers=other_user["headers"],
        )
        assert response.status_code == 403
