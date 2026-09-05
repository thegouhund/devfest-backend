"""Task 16: endpoint anomali (daftar, detail, tandai).

Acceptance criteria under test:
- Listing menghormati accessible_profile_ids
- Detail memuat perbandingan baseline dan aktivitas terkait bila ada
- Transisi status terbatas: new -> acknowledged | dismissed
- Anggota lain tidak bisa mengubah status yang bukan miliknya
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import ActivityLog, Anomaly, MeasurementSession, FamilyMember


ANOMALIES = "/api/v1/anomalies"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def make_anomaly(db, family_member_id: uuid.UUID, now: datetime, **overrides) -> Anomaly:
    anomaly = Anomaly(
        family_member_id=family_member_id,
        metric_type=overrides.pop("metric_type", "heart_rate"),
        observed_value=overrides.pop("observed_value", 98.5),
        baseline_mean=70.8,
        baseline_stddev=5.2,
        deviation_score=overrides.pop("deviation_score", 2.7),
        severity=overrides.pop("severity", "medium"),
        status=overrides.pop("status", "new"),
        detected_at=overrides.pop("detected_at", now),
        **overrides,
    )
    db.add(anomaly)
    db.commit()
    return anomaly


# --- Daftar ----------------------------------------------------------------


class TestList:
    def test_lists_own_anomalies(self, client, keluarga, db_session, now) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(ANOMALIES, headers=keluarga["ayah"]["headers"]).json()
        assert body["total"] == 1

    def test_response_shape(self, client, keluarga, db_session, now) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        item = client.get(ANOMALIES, headers=keluarga["ayah"]["headers"]).json()[
            "anomalies"
        ][0]
        assert {
            "id",
            "family_member_id",
            "metric_type",
            "observed_value",
            "baseline_mean",
            "deviation_score",
            "severity",
            "status",
            "detected_at",
        } <= set(item)

    def test_newest_first(self, client, keluarga, db_session, now) -> None:
        for hari in range(3):
            make_anomaly(
                db_session,
                keluarga["ayah"]["id"],
                now,
                detected_at=now - timedelta(days=hari),
            )
        items = client.get(ANOMALIES, headers=keluarga["ayah"]["headers"]).json()[
            "anomalies"
        ]
        stamps = [a["detected_at"] for a in items]
        assert stamps == sorted(stamps, reverse=True)

    def test_filters_by_status(self, client, keluarga, db_session, now) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now, status="new")
        make_anomaly(db_session, keluarga["ayah"]["id"], now, status="dismissed")

        body = client.get(
            f"{ANOMALIES}?status=new", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["total"] == 1

    def test_filters_by_metric(self, client, keluarga, db_session, now) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now, metric_type="heart_rate")
        make_anomaly(
            db_session, keluarga["ayah"]["id"], now, metric_type="respiration_rate"
        )

        body = client.get(
            f"{ANOMALIES}?metric_type=heart_rate",
            headers=keluarga["ayah"]["headers"],
        ).json()
        assert body["total"] == 1

    def test_filters_by_date_range(self, client, keluarga, db_session, now) -> None:
        from urllib.parse import quote

        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        make_anomaly(
            db_session,
            keluarga["ayah"]["id"],
            now,
            detected_at=now - timedelta(days=30),
        )

        start = quote((now - timedelta(days=7)).isoformat())
        body = client.get(
            f"{ANOMALIES}?start={start}", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["total"] == 1

    def test_pagination(self, client, keluarga, db_session, now) -> None:
        for _ in range(3):
            make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(
            f"{ANOMALIES}?limit=2", headers=keluarga["ayah"]["headers"]
        ).json()
        assert len(body["anomalies"]) == 2
        assert body["total"] == 3

    def test_requires_authentication(self, client) -> None:
        assert client.get(ANOMALIES).status_code == 401


class TestListVisibility:
    def test_family_member_visible_by_default(
        self, client, keluarga, db_session, now
    ) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(
            f"{ANOMALIES}?family_member_id={keluarga['ayah']['id']}",
            headers=keluarga["ibu"]["headers"],
        ).json()
        assert body["total"] == 1

    def test_private_vitals_hide_anomalies(
        self, client, keluarga, db_session, now
    ) -> None:
        """Anomali berasal dari data vital, jadi ikut setelan privasi vitals —
        kalau tidak, angka yang disembunyikan bocor lewat alert-nya."""
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        client.put(
            "/api/v1/settings/visibility",
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga["ayah"]["headers"],
        )
        response = client.get(
            f"{ANOMALIES}?family_member_id={keluarga['ayah']['id']}",
            headers=keluarga["ibu"]["headers"],
        )
        assert response.status_code == 403

    def test_outsider_forbidden(self, client, keluarga, db_session, now) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.get(
            f"{ANOMALIES}?family_member_id={keluarga['ayah']['id']}",
            headers=keluarga["luar"]["headers"],
        )
        assert response.status_code == 403

    def test_outsider_sees_own_empty_list(
        self, client, keluarga, db_session, now
    ) -> None:
        make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(ANOMALIES, headers=keluarga["luar"]["headers"]).json()
        assert body["total"] == 0

    def test_dependent_anomalies_visible_to_manager(
        self, client, keluarga, db_session, now
    ) -> None:
        make_anomaly(db_session, keluarga["anak"]["id"], now)
        body = client.get(ANOMALIES, headers=keluarga["ayah"]["headers"]).json()
        assert body["total"] == 1


# --- Detail ----------------------------------------------------------------


class TestDetail:
    def test_includes_baseline_comparison(
        self, client, keluarga, db_session, now
    ) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(
            f"{ANOMALIES}/{anomaly.id}", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["baseline_mean"] == pytest.approx(70.8)
        assert body["baseline_stddev"] == pytest.approx(5.2)

    def test_includes_related_activity(
        self, client, keluarga, db_session, now
    ) -> None:
        """Konteks penyebab (FR-3.3) supaya user tahu kemungkinan pemicunya."""
        activity = ActivityLog(
            family_member_id=keluarga["ayah"]["id"],
            logged_by_family_member_id=keluarga["ayah"]["id"],
            category="coffee",
            quantity=3,
            unit="cups",
            source="menu",
            occurred_at=now - timedelta(minutes=30),
        )
        db_session.add(activity)
        db_session.flush()
        anomaly = make_anomaly(
            db_session, keluarga["ayah"]["id"], now, related_activity_id=activity.id
        )

        body = client.get(
            f"{ANOMALIES}/{anomaly.id}", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["related_activity"]["category"] == "coffee"
        assert body["related_activity"]["quantity"] == 3

    def test_activity_null_when_absent(
        self, client, keluarga, db_session, now
    ) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        body = client.get(
            f"{ANOMALIES}/{anomaly.id}", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["related_activity"] is None

    def test_includes_session_id(self, client, keluarga, db_session, now) -> None:
        session = MeasurementSession(
            family_member_id=keluarga["ayah"]["id"],
            initiated_by_family_member_id=keluarga["ayah"]["id"],
            capture_method="upload",
            started_at=now,
            processing_status="completed",
        )
        db_session.add(session)
        db_session.flush()
        anomaly = make_anomaly(
            db_session,
            keluarga["ayah"]["id"],
            now,
            measurement_session_id=session.id,
        )

        body = client.get(
            f"{ANOMALIES}/{anomaly.id}", headers=keluarga["ayah"]["headers"]
        ).json()
        assert body["measurement_session_id"] == str(session.id)

    def test_outsider_cannot_read(self, client, keluarga, db_session, now) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.get(
            f"{ANOMALIES}/{anomaly.id}", headers=keluarga["luar"]["headers"]
        )
        assert response.status_code == 404

    def test_unknown_id_404(self, client, keluarga) -> None:
        response = client.get(
            f"{ANOMALIES}/{uuid.uuid4()}", headers=keluarga["ayah"]["headers"]
        )
        assert response.status_code == 404


# --- Perubahan status ------------------------------------------------------


class TestUpdateStatus:
    @pytest.mark.parametrize("target", ["acknowledged", "dismissed"])
    def test_owner_can_set_allowed_status(
        self, client, keluarga, db_session, now, target
    ) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": target},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == target

    def test_cannot_revert_to_new(self, client, keluarga, db_session, now) -> None:
        """Kembali ke `new` akan membuat anomali lama muncul lagi sebagai
        belum dibaca — dan schema memang hanya mengizinkan dua nilai."""
        anomaly = make_anomaly(
            db_session, keluarga["ayah"]["id"], now, status="acknowledged"
        )
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "new"},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 422

    def test_invalid_status_rejected(self, client, keluarga, db_session, now) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "diabaikan-selamanya"},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 422

    def test_family_member_cannot_change(
        self, client, keluarga, db_session, now
    ) -> None:
        """Boleh melihat bukan berarti boleh menandai sudah dibaca."""
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "dismissed"},
            headers=keluarga["ibu"]["headers"],
        )
        assert response.status_code == 403

    def test_manager_can_change_dependent_anomaly(
        self, client, keluarga, db_session, now
    ) -> None:
        anomaly = make_anomaly(db_session, keluarga["anak"]["id"], now)
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "acknowledged"},
            headers=keluarga["ayah"]["headers"],
        )
        assert response.status_code == 200

    def test_outsider_cannot_change(self, client, keluarga, db_session, now) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        response = client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "dismissed"},
            headers=keluarga["luar"]["headers"],
        )
        assert response.status_code in (403, 404)

    def test_status_persists(self, client, keluarga, db_session, now) -> None:
        anomaly = make_anomaly(db_session, keluarga["ayah"]["id"], now)
        client.patch(
            f"{ANOMALIES}/{anomaly.id}",
            json={"status": "acknowledged"},
            headers=keluarga["ayah"]["headers"],
        )
        db_session.expire_all()
        assert db_session.get(Anomaly, anomaly.id).status == "acknowledged"


# --- Integrasi dashboard ---------------------------------------------------


class TestDashboardCount:
    def test_open_anomalies_counted(self, client, keluarga, db_session, now) -> None:
        """Angka di dashboard keluarga (Task 11) harus ikut terisi."""
        make_anomaly(db_session, keluarga["ayah"]["id"], now, status="new")
        make_anomaly(db_session, keluarga["ayah"]["id"], now, status="dismissed")

        members = client.get(
            "/api/v1/profiles/dashboard/family",
            headers=keluarga["ayah"]["headers"],
        ).json()["members"]
        ayah = next(m for m in members if m["full_name"] == "Ayah")
        assert ayah["open_anomalies"] == 1
