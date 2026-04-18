"""Tests for job queue: SQLite DB operations and API endpoints."""

from __future__ import annotations

import pytest

from spacetime.api.jobs import (
    JobDB,
    JobSubmission,
    _compute_bbox_area_km2,
    _compute_n_months,
    _detect_utm_epsg,
)

# --- Helper fixtures ---


@pytest.fixture()
def job_db(tmp_path):
    """Create a JobDB backed by a temp SQLite file."""
    return JobDB(db_path=tmp_path / "test_jobs.sqlite")


def _valid_submission(**overrides) -> JobSubmission:
    """Create a valid JobSubmission with optional overrides."""
    defaults = {
        "aoi_name": "test_aoi",
        "bbox_wgs84": [5.40, 22.70, 5.65, 22.95],
        "start_month": "2024-01",
        "end_month": "2024-06",
        "chunk_size": 512,
    }
    defaults.update(overrides)
    return JobSubmission(**defaults)


# --- Helper function tests ---


@pytest.mark.unit
def test_compute_bbox_area():
    area = _compute_bbox_area_km2(5.40, 22.70, 5.65, 22.95)
    assert 500 < area < 900  # ~25km x 25km = ~625 km²


@pytest.mark.unit
def test_compute_n_months():
    assert _compute_n_months("2024-01", "2024-06") == 6
    assert _compute_n_months("2024-01", "2024-01") == 1
    assert _compute_n_months("2023-01", "2024-12") == 24


@pytest.mark.unit
def test_detect_utm_epsg_northern():
    assert _detect_utm_epsg(22.8, 5.5) == 32631  # UTM 31N


@pytest.mark.unit
def test_detect_utm_epsg_southern():
    assert _detect_utm_epsg(-33.9, 18.4) == 32734  # UTM 34S


# --- Validation tests ---


@pytest.mark.unit
def test_submission_valid():
    sub = _valid_submission()
    assert sub.aoi_name == "test_aoi"


@pytest.mark.unit
def test_submission_bad_aoi_name():
    with pytest.raises(ValueError, match="alphanumeric"):
        _valid_submission(aoi_name="bad name!")


@pytest.mark.unit
def test_submission_short_aoi_name():
    with pytest.raises(ValueError):
        _valid_submission(aoi_name="ab")


@pytest.mark.unit
def test_submission_bad_month_format():
    with pytest.raises(ValueError, match="YYYY-MM"):
        _valid_submission(start_month="2024/01")


@pytest.mark.unit
def test_submission_date_range_exceeded():
    with pytest.raises(ValueError, match="36 months"):
        _valid_submission(start_month="2020-01", end_month="2024-01")


@pytest.mark.unit
def test_submission_end_before_start():
    with pytest.raises(ValueError, match="after"):
        _valid_submission(start_month="2024-06", end_month="2024-01")


@pytest.mark.unit
def test_submission_bad_bbox():
    with pytest.raises(ValueError):
        _valid_submission(bbox_wgs84=[5.65, 22.70, 5.40, 22.95])  # west > east


# --- JobDB tests ---


@pytest.mark.unit
def test_submit_creates_pending_job(job_db):
    sub = _valid_submission()
    job_id = job_db.submit(sub, "tr_dev_test")
    assert job_id is not None

    job = job_db.get(job_id)
    assert job is not None
    assert job.status == "pending"
    assert job.aoi_name == "test_aoi"
    assert job.epsg is not None


@pytest.mark.unit
def test_submit_rejects_large_bbox(job_db):
    with pytest.raises(ValueError, match="10000"):
        job_db.submit(
            _valid_submission(bbox_wgs84=[-20.0, 10.0, 20.0, 50.0]),
            "tr_dev_test",
        )


@pytest.mark.unit
def test_claim_returns_oldest(job_db):
    sub1 = _valid_submission(aoi_name="first_aoi")
    sub2 = _valid_submission(aoi_name="second_aoi")
    job_db.submit(sub1, "tr_dev_test")
    job_db.submit(sub2, "tr_dev_test")

    claimed = job_db.claim("worker_1")
    assert claimed is not None
    assert claimed.aoi_name == "first_aoi"
    assert claimed.status == "claimed"


@pytest.mark.unit
def test_claim_no_pending_returns_none(job_db):
    assert job_db.claim("worker_1") is None


@pytest.mark.unit
def test_claim_is_atomic(job_db):
    """Two claims on the same pending job — only one succeeds."""
    job_db.submit(_valid_submission(), "tr_dev_test")

    first = job_db.claim("worker_1")
    second = job_db.claim("worker_2")

    assert first is not None
    assert second is None


@pytest.mark.unit
def test_update_status(job_db):
    job_id = job_db.submit(_valid_submission(), "tr_dev_test")
    job_db.claim("worker_1")

    job_db.update_status(job_id, "running")
    job = job_db.get(job_id)
    assert job.status == "running"

    job_db.update_status(job_id, "done", store_path="/data/stores/test")
    job = job_db.get(job_id)
    assert job.status == "done"
    assert job.store_path == "/data/stores/test"
    assert job.completed_at is not None


@pytest.mark.unit
def test_update_status_failed(job_db):
    job_id = job_db.submit(_valid_submission(), "tr_dev_test")
    job_db.update_status(job_id, "failed", error="Pipeline crashed")

    job = job_db.get(job_id)
    assert job.status == "failed"
    assert job.error == "Pipeline crashed"
    assert job.completed_at is not None


@pytest.mark.unit
def test_list_for_key(job_db):
    job_db.submit(_valid_submission(aoi_name="aoi_a"), "tr_dev_user1")
    job_db.submit(_valid_submission(aoi_name="aoi_b"), "tr_dev_user1")
    job_db.submit(_valid_submission(aoi_name="aoi_c"), "tr_dev_user2")

    user1_jobs = job_db.list_for_key("tr_dev_user1")
    assert len(user1_jobs) == 2

    user2_jobs = job_db.list_for_key("tr_dev_user2")
    assert len(user2_jobs) == 1
    assert user2_jobs[0].aoi_name == "aoi_c"


# --- API endpoint tests ---


@pytest.mark.unit
def test_post_job(api_client, monkeypatch):
    """POST /v1/jobs creates a job."""
    monkeypatch.setenv("TILERIPPER_WORKER_KEY", "test_worker_key")

    r = api_client.post(
        "/v1/jobs",
        json={
            "aoi_name": "test_submit",
            "bbox_wgs84": [5.40, 22.70, 5.65, 22.95],
            "start_month": "2024-01",
            "end_month": "2024-06",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["aoi_name"] == "test_submit"
    assert data["status"] == "pending"


@pytest.mark.unit
def test_post_job_oversized_bbox(api_client):
    """POST /v1/jobs with huge bbox returns 400."""
    r = api_client.post(
        "/v1/jobs",
        json={
            "aoi_name": "too_big",
            "bbox_wgs84": [-20.0, 10.0, 20.0, 50.0],
            "start_month": "2024-01",
            "end_month": "2024-06",
        },
    )
    assert r.status_code in (400, 422, 500)


@pytest.mark.unit
def test_get_jobs_next_no_worker_key(api_client):
    """GET /v1/jobs/next without worker key returns error."""
    r = api_client.get("/v1/jobs/next")
    assert r.status_code in (401, 403, 503)
