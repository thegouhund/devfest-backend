"""Task 11: endpoint statistik vital sign.

Acceptance criteria under test:
- Trend menerima metric_type, start, end, bucket; balik bucket terurut
- Summary memuat mean/min/max, baseline aktif, dan perbandingan periode sebelumnya
- Dashboard keluarga hanya memuat anggota yang datanya boleh dilihat pemanggil
- Meminta user di luar accessible_profile_ids -> 403, bukan hasil separuh
- metric_type tak dikenal -> 400 lewat lookup FK, bukan daftar hardcode
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Baseline, MeasurementSession, FamilyMember, VitalsReading


TREND = "/api/v1/vitals/trend"
SUMMARY = "/api/v1/vitals/summary"
DASHBOARD = "/api/v1/profiles/dashboard/family"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def add_readings(
    db,
    family_member_id: uuid.UUID,
    values: list[tuple[datetime, float]],
    metric="heart_rate",
) -> None:
    """Tulis pembacaan langsung, tanpa lewat pipeline rPPG."""
    session = MeasurementSession(
        family_member_id=family_member_id,
        initiated_by_family_member_id=family_member_id,
        capture_method="upload",
        started_at=values[0][0],
        processing_status="completed",
    )
    db.add(session)
    db.flush()
    for moment, value in values:
        db.add(
            VitalsReading(
                measurement_session_id=session.id,
                family_member_id=family_member_id,
                recorded_at=moment,
                metric_type=metric,
                value=value,
                unit="bpm",
            )
        )
    db.commit()


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def keluarga_dengan_data(keluarga, db_session, now):
    """Keluarga standar plus riwayat pembacaan untuk ayah dan ibu."""
    add_readings(
        db_session,
        keluarga["ayah"]["id"],
        [(now - timedelta(days=d), 70 + d) for d in range(5)],
    )
    add_readings(db_session, keluarga["ibu"]["id"], [(now - timedelta(days=1), 85.0)])
    return keluarga


# --- Trend -----------------------------------------------------------------


class TestTrend:
    def test_returns_buckets(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["metric_type"] == "heart_rate"
        assert body["unit"] == "bpm"
        assert len(body["buckets"]) == 5

    def test_buckets_are_chronological(self, client, keluarga_dengan_data, now) -> None:
        buckets = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["buckets"]
        stamps = [b["bucket"] for b in buckets]
        assert stamps == sorted(stamps)

    def test_bucket_carries_aggregates(self, client, keluarga_dengan_data, now) -> None:
        buckets = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["buckets"]
        first = buckets[0]
        assert {"bucket", "avg", "min", "max", "count"} <= set(first)

    def test_empty_periods_are_omitted(self, client, keluarga_dengan_data, now) -> None:
        """Bucket tanpa data tidak dikirim (kontrak API) — frontend harus
        menangani deret yang berlubang."""
        buckets = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=90)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["buckets"]
        assert len(buckets) == 5, "hanya hari yang ada datanya"

    def test_range_is_respected(self, client, keluarga_dengan_data, now) -> None:
        buckets = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=2)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["buckets"]
        assert len(buckets) == 3

    @pytest.mark.parametrize("bucket", ["day", "week", "month"])
    def test_bucket_sizes_accepted(self, client, keluarga_dengan_data, now, bucket) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
                "bucket": bucket,
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 200

    def test_unknown_metric_rejected(self, client, keluarga_dengan_data, now) -> None:
        """Divalidasi lewat tabel metric_types, bukan daftar hardcode —
        metrik baru cukup ditambah satu baris."""
        response = client.get(
            TREND,
            params={
                "metric_type": "belum_ada",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 400

    def test_invalid_bucket_rejected(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now),
                "bucket": "abad",
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 422

    def test_end_before_start_rejected(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now),
                "end": iso(now - timedelta(days=7)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 400

    def test_requires_authentication(self, client, now) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now),
                "end": iso(now),
            },
        )
        assert response.status_code == 401


class TestTrendVisibility:
    def test_can_read_family_member(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
                "family_member_id": str(keluarga_dengan_data["ibu"]["id"]),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 200
        assert response.json()["buckets"]

    def test_outsider_forbidden(self, client, keluarga_dengan_data, now) -> None:
        """Data keluarga lain tidak boleh terbaca sama sekali."""
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
                "family_member_id": str(keluarga_dengan_data["ayah"]["id"]),
            },
            headers=keluarga_dengan_data["luar"]["headers"],
        )
        assert response.status_code == 403

    def test_private_member_forbidden(self, client, keluarga_dengan_data, now) -> None:
        """Dilihat dari sesama anggota, bukan dari admin: admin memang boleh
        melihat seluruh profil di akunnya (FR-6.4)."""
        client.put(
            "/api/v1/settings/visibility",
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga_dengan_data["ibu"]["headers"],
        )
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
                "family_member_id": str(keluarga_dengan_data["ibu"]["id"]),
            },
            headers=keluarga_dengan_data["anak"]["headers"],
        )
        assert response.status_code == 403

    def test_forbidden_not_partial_result(self, client, keluarga_dengan_data, now) -> None:
        """403 utuh, bukan 200 dengan bucket kosong — hasil kosong terbaca
        seolah orangnya belum pernah mengukur."""
        response = client.get(
            TREND,
            params={
                "metric_type": "heart_rate",
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
                "family_member_id": str(keluarga_dengan_data["ayah"]["id"]),
            },
            headers=keluarga_dengan_data["luar"]["headers"],
        )
        assert response.status_code == 403
        assert "buckets" not in response.json()


# --- Summary ---------------------------------------------------------------


class TestSummary:
    def test_returns_metrics(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        )
        assert response.status_code == 200, response.text
        metrics = {m["metric_type"]: m for m in response.json()["metrics"]}
        assert "heart_rate" in metrics

    def test_aggregates_are_correct(self, client, keluarga_dengan_data, now, db_session) -> None:
        """Nilai 70..74 -> avg 72, min 70, max 74."""
        metrics = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["metrics"]
        hr = next(m for m in metrics if m["metric_type"] == "heart_rate")
        assert hr["avg"] == pytest.approx(72.0)
        assert hr["min"] == pytest.approx(70.0)
        assert hr["max"] == pytest.approx(74.0)
        assert hr["count"] == 5

    def test_baseline_included_when_active(
        self, client, keluarga_dengan_data, now, db_session
    ) -> None:
        db_session.add(
            Baseline(
                family_member_id=keluarga_dengan_data["ayah"]["id"],
                metric_type="heart_rate",
                mean_value=70.8,
                stddev_value=5.2,
                sample_count=30,
                window_start=now - timedelta(days=30),
                window_end=now,
                is_active=True,
            )
        )
        db_session.commit()

        metrics = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["metrics"]
        hr = next(m for m in metrics if m["metric_type"] == "heart_rate")
        assert hr["baseline"]["is_active"] is True
        assert hr["baseline"]["mean"] == pytest.approx(70.8)

    def test_baseline_absent_reads_as_collecting(self, client, keluarga_dengan_data, now) -> None:
        """Tanpa baseline aktif, frontend menampilkan 'sedang mengumpulkan
        data' — bukan angka nol yang menyesatkan."""
        metrics = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["metrics"]
        hr = next(m for m in metrics if m["metric_type"] == "heart_rate")
        assert hr["baseline"] is None or hr["baseline"]["is_active"] is False

    def test_previous_period_comparison(self, client, keluarga_dengan_data, now, db_session) -> None:
        """Periode sebelumnya dihitung dari rentang yang sama panjangnya."""
        add_readings(
            db_session,
            keluarga_dengan_data["ayah"]["id"],
            [(now - timedelta(days=10), 80.0)],
        )
        metrics = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["metrics"]
        hr = next(m for m in metrics if m["metric_type"] == "heart_rate")
        assert hr["previous_period"] is not None
        assert hr["previous_period"]["avg"] == pytest.approx(80.0)

    def test_no_previous_data_gives_null(self, client, keluarga_dengan_data, now) -> None:
        metrics = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now + timedelta(days=1)),
            },
            headers=keluarga_dengan_data["ayah"]["headers"],
        ).json()["metrics"]
        hr = next(m for m in metrics if m["metric_type"] == "heart_rate")
        assert hr["previous_period"] is None

    def test_outsider_forbidden(self, client, keluarga_dengan_data, now) -> None:
        response = client.get(
            SUMMARY,
            params={
                "start": iso(now - timedelta(days=7)),
                "end": iso(now),
                "family_member_id": str(keluarga_dengan_data["ayah"]["id"]),
            },
            headers=keluarga_dengan_data["luar"]["headers"],
        )
        assert response.status_code == 403


# --- Dashboard keluarga ----------------------------------------------------


class TestFamilyDashboard:
    """Dashboard keluarga sekarang implisit per akun — tidak ada lagi id
    family di URL, karena batas keluarga adalah akun yang sedang login."""

    def test_lists_visible_members(self, client, keluarga_dengan_data) -> None:
        response = client.get(
            DASHBOARD, headers=keluarga_dengan_data["ayah"]["headers"]
        )
        assert response.status_code == 200, response.text
        names = {m["full_name"] for m in response.json()["members"]}
        assert names == {"Ayah", "Ibu", "Anak"}

    def test_shows_latest_reading(self, client, keluarga_dengan_data) -> None:
        members = client.get(
            DASHBOARD, headers=keluarga_dengan_data["ayah"]["headers"]
        ).json()["members"]
        ibu = next(m for m in members if m["full_name"] == "Ibu")
        latest = {r["metric_type"]: r["value"] for r in ibu["latest"]}
        assert latest["heart_rate"] == pytest.approx(85.0)

    def test_private_member_hidden_from_sibling(
        self, client, keluarga_dengan_data
    ) -> None:
        """Anggota yang menyetel privat tidak muncul sama sekali bagi sesama
        anggota, bukan muncul dengan data kosong (kontrak API)."""
        client.put(
            "/api/v1/settings/visibility",
            json={"data_type": "vitals", "visibility": "private"},
            headers=keluarga_dengan_data["ibu"]["headers"],
        )
        members = client.get(
            DASHBOARD, headers=keluarga_dengan_data["anak"]["headers"]
        ).json()["members"]
        assert "Ibu" not in {m["full_name"] for m in members}

    def test_other_account_sees_only_itself(
        self, client, keluarga_dengan_data
    ) -> None:
        """Akun lain tidak diblokir — dia hanya melihat keluarganya sendiri,
        yang kebetulan cuma dirinya."""
        members = client.get(
            DASHBOARD, headers=keluarga_dengan_data["luar"]["headers"]
        ).json()["members"]
        assert {m["full_name"] for m in members} == {"Orang Luar"}

    def test_requires_authentication(self, client) -> None:
        assert client.get(DASHBOARD).status_code == 401

    def test_member_without_readings_still_listed(
        self, client, keluarga_dengan_data
    ) -> None:
        """Anggota baru yang belum pernah mengukur tetap muncul dengan
        daftar kosong — supaya keluarga tahu dia belum mulai."""
        members = client.get(
            DASHBOARD, headers=keluarga_dengan_data["ayah"]["headers"]
        ).json()["members"]
        entry = next(m for m in members if m["full_name"] == "Anak")
        assert entry["latest"] == []
        assert entry["last_measurement_at"] is None
