"""Task 4: password hashing, JWT, dependency current-user.

Acceptance criteria under test:
- hash_password/verify_password round-trip; hash bergaram dan beda tiap panggilan
- Token membawa sub & exp; token kedaluwarsa atau dipalsukan -> 401
- User dependent tidak bisa dapat token
- Helper require_family_admin untuk route khusus admin
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.security import (
    authenticate_user,
    create_access_token,
    decode_access_token,
    hash_password,
    require_family_admin,
    resolve_current_user,
    verify_password,
)
from app.db.models import Base, Family, FamilyMember, User


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    monkeypatch.setenv("JWT_SECRET", "test-secret-key-for-signing")
    get_settings.cache_clear()
    yield "test-secret-key-for-signing"
    get_settings.cache_clear()


# --- Hashing ---------------------------------------------------------------


class TestPasswordHashing:
    def test_round_trip(self) -> None:
        hashed = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", hashed)

    def test_wrong_password_rejected(self) -> None:
        hashed = hash_password("benar")
        assert not verify_password("salah", hashed)

    def test_hash_is_not_plaintext(self) -> None:
        assert "rahasia" not in hash_password("rahasia")

    def test_salted_so_same_password_differs(self) -> None:
        """Tanpa garam, dua user berpassword sama punya hash identik —
        satu hash bocor langsung membocorkan keduanya."""
        assert hash_password("sama") != hash_password("sama")

    def test_verify_handles_malformed_hash(self) -> None:
        """Hash rusak harus jadi False, bukan melempar exception yang
        bisa jatuh jadi 500 di endpoint login."""
        assert not verify_password("apa pun", "bukan-hash-valid")

    def test_long_password_accepted(self) -> None:
        """bcrypt memotong di 72 byte; implementasi tidak boleh error."""
        long_password = "a" * 200
        assert verify_password(long_password, hash_password(long_password))


# --- JWT -------------------------------------------------------------------


class TestAccessToken:
    def test_token_carries_subject(self, secret) -> None:
        user_id = uuid.uuid4()
        payload = decode_access_token(create_access_token(user_id))
        assert payload["sub"] == str(user_id)

    def test_token_carries_expiry(self, secret) -> None:
        payload = decode_access_token(create_access_token(uuid.uuid4()))
        assert "exp" in payload

    def test_expired_token_rejected(self, secret) -> None:
        expired = create_access_token(uuid.uuid4(), expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc:
            decode_access_token(expired)
        assert exc.value.status_code == 401

    def test_tampered_token_rejected(self, secret) -> None:
        token = create_access_token(uuid.uuid4())
        head, payload, signature = token.split(".")
        forged = f"{head}.{payload}.{signature[:-4]}xxxx"
        with pytest.raises(HTTPException) as exc:
            decode_access_token(forged)
        assert exc.value.status_code == 401

    def test_token_signed_with_other_secret_rejected(
        self, secret, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token dari secret lain tidak boleh diterima."""
        from app.core.config import get_settings

        token = create_access_token(uuid.uuid4())
        monkeypatch.setenv("JWT_SECRET", "secret-yang-berbeda")
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc:
            decode_access_token(token)
        assert exc.value.status_code == 401

    def test_token_without_expiry_rejected(self, secret) -> None:
        """Token tanpa exp akan berlaku selamanya kalau diterima."""
        import jwt as pyjwt

        from app.core.security import ALGORITHM

        forever = pyjwt.encode(
            {"sub": str(uuid.uuid4())}, secret, algorithm=ALGORITHM
        )
        with pytest.raises(HTTPException) as exc:
            decode_access_token(forever)
        assert exc.value.status_code == 401

    def test_unsigned_token_rejected(self, secret) -> None:
        """Serangan alg=none: token tanpa tanda tangan harus ditolak."""
        import jwt as pyjwt

        unsigned = pyjwt.encode({"sub": str(uuid.uuid4())}, "", algorithm="none")
        with pytest.raises(HTTPException) as exc:
            decode_access_token(unsigned)
        assert exc.value.status_code == 401

    def test_garbage_token_rejected(self, secret) -> None:
        with pytest.raises(HTTPException) as exc:
            decode_access_token("bukan.token.jwt")
        assert exc.value.status_code == 401

    def test_missing_secret_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JWT_SECRET kosong harus error jelas, bukan diam-diam
        menandatangani dengan string kosong."""
        from app.core.config import get_settings

        monkeypatch.setenv("JWT_SECRET", "")
        get_settings.cache_clear()
        with pytest.raises(Exception) as exc:
            create_access_token(uuid.uuid4())
        assert "JWT_SECRET" in str(exc.value)
        get_settings.cache_clear()


# --- Autentikasi user ------------------------------------------------------


class TestAuthenticateUser:
    def test_valid_credentials(self, db) -> None:
        db.add(
            User(
                full_name="Budi",
                email="budi@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()
        assert authenticate_user(db, "budi@example.com", "rahasia123") is not None

    def test_wrong_password(self, db) -> None:
        db.add(
            User(
                full_name="Budi",
                email="budi@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()
        assert authenticate_user(db, "budi@example.com", "salah") is None

    def test_unknown_email(self, db) -> None:
        assert authenticate_user(db, "tidakada@example.com", "apa pun") is None

    def test_dependent_cannot_authenticate(self, db) -> None:
        """ERD §2.1: dependent tidak punya password_hash, jadi tidak boleh
        bisa login dengan cara apa pun."""
        admin = User(full_name="Admin", email="admin@example.com")
        db.add(admin)
        db.flush()
        db.add(
            User(
                full_name="Anak",
                email="anak@example.com",
                is_dependent=True,
                managed_by_user_id=admin.id,
                password_hash=None,
            )
        )
        db.commit()
        assert authenticate_user(db, "anak@example.com", "") is None

    def test_unknown_email_takes_similar_time(self, db) -> None:
        """Waktu respons tidak boleh membocorkan email mana yang terdaftar.

        Tanpa dummy-verify, email tak dikenal balik ~1000x lebih cepat karena
        melewati bcrypt — cukup untuk memetakan siapa saja yang punya akun.
        """
        import statistics
        import time

        db.add(
            User(
                full_name="Ada",
                email="ada@example.com",
                password_hash=hash_password("rahasia123"),
            )
        )
        db.commit()

        def median_ms(email: str) -> float:
            samples = []
            for _ in range(5):
                start = time.perf_counter()
                authenticate_user(db, email, "tebakan-salah")
                samples.append(time.perf_counter() - start)
            return statistics.median(samples) * 1000

        known = median_ms("ada@example.com")
        unknown = median_ms("tidakada@example.com")
        assert 0.2 < unknown / known < 5, (
            f"selisih waktu terlalu mencolok: terdaftar {known:.1f}ms vs "
            f"tidak terdaftar {unknown:.1f}ms"
        )

    def test_inactive_user_cannot_authenticate(self, db) -> None:
        db.add(
            User(
                full_name="Nonaktif",
                email="off@example.com",
                password_hash=hash_password("rahasia123"),
                is_active=False,
            )
        )
        db.commit()
        assert authenticate_user(db, "off@example.com", "rahasia123") is None


# --- Dependency current user -----------------------------------------------


class TestResolveCurrentUser:
    def test_valid_token_returns_user(self, db, secret) -> None:
        user = User(
            full_name="Budi",
            email="budi@example.com",
            password_hash=hash_password("x"),
        )
        db.add(user)
        db.commit()
        resolved = resolve_current_user(db, create_access_token(user.id))
        assert resolved.id == user.id

    def test_token_for_deleted_user_rejected(self, db, secret) -> None:
        """Token valid tapi user sudah tidak ada -> 401, bukan 500."""
        with pytest.raises(HTTPException) as exc:
            resolve_current_user(db, create_access_token(uuid.uuid4()))
        assert exc.value.status_code == 401

    def test_token_for_deactivated_user_rejected(self, db, secret) -> None:
        """User dinonaktifkan setelah token terbit harus langsung ditolak."""
        user = User(
            full_name="Budi",
            email="budi@example.com",
            password_hash=hash_password("x"),
        )
        db.add(user)
        db.commit()
        token = create_access_token(user.id)
        user.is_active = False
        db.commit()
        with pytest.raises(HTTPException) as exc:
            resolve_current_user(db, token)
        assert exc.value.status_code == 401


# --- Otorisasi admin family ------------------------------------------------


class TestRequireFamilyAdmin:
    @pytest.fixture
    def family_setup(self, db):
        admin = User(
            full_name="Admin", email="a@x.com", password_hash=hash_password("x")
        )
        member = User(
            full_name="Member", email="m@x.com", password_hash=hash_password("x")
        )
        outsider = User(
            full_name="Orang Luar", email="o@x.com", password_hash=hash_password("x")
        )
        db.add_all([admin, member, outsider])
        db.flush()
        family = Family(name="Keluarga", invite_code="KODE", created_by=admin.id)
        db.add(family)
        db.flush()
        db.add_all(
            [
                FamilyMember(family_id=family.id, user_id=admin.id, role="admin"),
                FamilyMember(family_id=family.id, user_id=member.id, role="member"),
            ]
        )
        db.commit()
        return family, admin, member, outsider

    def test_admin_allowed(self, db, family_setup) -> None:
        family, admin, _, _ = family_setup
        require_family_admin(db, admin, family.id)

    def test_member_forbidden(self, db, family_setup) -> None:
        family, _, member, _ = family_setup
        with pytest.raises(HTTPException) as exc:
            require_family_admin(db, member, family.id)
        assert exc.value.status_code == 403

    def test_outsider_forbidden(self, db, family_setup) -> None:
        family, _, _, outsider = family_setup
        with pytest.raises(HTTPException) as exc:
            require_family_admin(db, outsider, family.id)
        assert exc.value.status_code == 403

    def test_removed_admin_forbidden(self, db, family_setup) -> None:
        """Admin yang statusnya removed tidak boleh tetap punya hak admin."""
        family, admin, _, _ = family_setup
        membership = (
            db.query(FamilyMember)
            .filter_by(family_id=family.id, user_id=admin.id)
            .one()
        )
        membership.status = "removed"
        db.commit()
        with pytest.raises(HTTPException) as exc:
            require_family_admin(db, admin, family.id)
        assert exc.value.status_code == 403
