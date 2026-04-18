"""SQLite-backed job queue for async AOI processing.

Uses WAL mode for concurrent reader safety across multiple uvicorn workers.
Writes are serialized by SQLite's locking — fine for low-volume job queues.
"""

from __future__ import annotations

import math
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

# Match the data root used by serve.py
DATA_ROOT = Path(__file__).resolve().parents[3] / "data"

# --- SQL Schema ---

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    api_key TEXT,
    aoi_name TEXT NOT NULL,
    bbox_west REAL,
    bbox_south REAL,
    bbox_east REAL,
    bbox_north REAL,
    epsg INTEGER,
    start_month TEXT,
    end_month TEXT,
    chunk_size INTEGER DEFAULT 512,
    created_at TEXT DEFAULT (datetime('now')),
    claimed_at TEXT,
    completed_at TEXT,
    worker_id TEXT,
    error TEXT,
    store_path TEXT,
    n_months INTEGER,
    bbox_area_km2 REAL
);
"""

# --- Pydantic Models ---


class JobSubmission(BaseModel):
    """Request body for submitting a new processing job."""

    aoi_name: str = Field(..., min_length=3, max_length=50)
    bbox_wgs84: list[float] = Field(..., min_length=4, max_length=4)
    start_month: str  # YYYY-MM
    end_month: str  # YYYY-MM
    chunk_size: int = Field(default=512, ge=128, le=4096)

    @field_validator("aoi_name")
    @classmethod
    def validate_aoi_name(cls, v: str) -> str:
        """Validate aoi_name: alphanumeric + underscores only."""
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError("aoi_name must contain only alphanumeric characters and underscores")
        return v

    @field_validator("bbox_wgs84")
    @classmethod
    def validate_bbox(cls, v: list[float]) -> list[float]:
        """Validate bbox bounds."""
        west, south, east, north = v
        if not (-180 <= west <= 180):
            raise ValueError("west must be between -180 and 180")
        if not (-180 <= east <= 180):
            raise ValueError("east must be between -180 and 180")
        if not (-90 <= south <= 90):
            raise ValueError("south must be between -90 and 90")
        if not (-90 <= north <= 90):
            raise ValueError("north must be between -90 and 90")
        if west >= east:
            raise ValueError("west must be less than east")
        if south >= north:
            raise ValueError("south must be less than north")
        return v

    @field_validator("start_month", "end_month")
    @classmethod
    def validate_month_format(cls, v: str) -> str:
        """Validate YYYY-MM format."""
        if not re.match(r"^\d{4}-\d{2}$", v):
            raise ValueError("month must be in YYYY-MM format")
        year = int(v[:4])
        month = int(v[5:7])
        if not (1 <= month <= 12):
            raise ValueError("month must be between 01 and 12")
        if not (2000 <= year <= 2100):
            raise ValueError("year must be between 2000 and 2100")
        return v

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        """Validate date range is not more than 36 months."""
        start_year = int(self.start_month[:4])
        start_mon = int(self.start_month[5:7])
        end_year = int(self.end_month[:4])
        end_mon = int(self.end_month[5:7])

        # Calculate months difference
        months_diff = (end_year - start_year) * 12 + (end_mon - start_mon)

        if months_diff < 0:
            raise ValueError("end_month must be after start_month")
        if months_diff > 36:
            raise ValueError("date range must not exceed 36 months")

        return self


class JobStatus(BaseModel):
    """Summary status of a job for listing."""

    id: str
    status: str
    aoi_name: str
    created_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


class JobDetail(BaseModel):
    """Full job details as stored in the database."""

    id: str
    status: str
    api_key: str | None = None
    aoi_name: str
    bbox_west: float | None = None
    bbox_south: float | None = None
    bbox_east: float | None = None
    bbox_north: float | None = None
    epsg: int | None = None
    start_month: str | None = None
    end_month: str | None = None
    chunk_size: int
    created_at: str | None = None
    claimed_at: str | None = None
    completed_at: str | None = None
    worker_id: str | None = None
    error: str | None = None
    store_path: str | None = None
    n_months: int | None = None
    bbox_area_km2: float | None = None


# --- Database Layer ---


def _compute_bbox_area_km2(west: float, south: float, east: float, north: float) -> float:
    """Compute approximate bbox area in km² using haversine formula.

    Uses the mean latitude for longitudinal distance calculation.
    """
    # Earth's radius in km
    R = 6371.0

    # Convert to radians
    lat1 = math.radians(south)
    lat2 = math.radians(north)
    lon1 = math.radians(west)
    lon2 = math.radians(east)
    mean_lat = (lat1 + lat2) / 2

    # North-south distance (meridian arc)
    ns_distance = R * (lat2 - lat1)

    # East-west distance at mean latitude (parallel arc)
    ew_distance = R * math.cos(mean_lat) * (lon2 - lon1)

    return abs(ns_distance * ew_distance)


def _compute_n_months(start_month: str, end_month: str) -> int:
    """Compute number of months inclusive."""
    start_year = int(start_month[:4])
    start_mon = int(start_month[5:7])
    end_year = int(end_month[:4])
    end_mon = int(end_month[5:7])

    return (end_year - start_year) * 12 + (end_mon - start_mon) + 1


def _detect_utm_epsg(lat: float, lng: float) -> int:
    """Auto-detect UTM zone EPSG code from lat/lng (center of bbox).

    Returns EPSG:326XX for northern hemisphere, EPSG:327XX for southern.
    """
    # UTM zone number: 1-60, zone 1 is -180 to -174
    zone = int((lng + 180) / 6) + 1
    zone = max(1, min(60, zone))  # clamp to valid range

    # Northern hemisphere: 326XX, Southern: 327XX
    if lat >= 0:
        return 32600 + zone
    else:
        return 32700 + zone


class JobDB:
    """SQLite-backed job queue with WAL mode for concurrent access."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        """Initialize the job database.

        Args:
            db_path: Path to SQLite database. Defaults to DATA_ROOT / "jobs.sqlite"
        """
        if db_path is None:
            db_path = DATA_ROOT / "jobs.sqlite"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self) -> None:
        """Initialize database with WAL mode and create tables."""
        with sqlite3.connect(str(self.db_path)) as conn:
            # Enable WAL mode for concurrent reader safety
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")

            # Create tables
            conn.execute(CREATE_JOBS_TABLE)
            conn.commit()

    def submit(self, job: JobSubmission, api_key: str) -> str:
        """Submit a new job to the queue.

        Args:
            job: The job submission data
            api_key: API key of the submitting user

        Returns:
            The generated job ID (UUID)

        Raises:
            ValueError: If bbox area exceeds 10000 km²
        """
        job_id = str(uuid.uuid4())

        # Compute derived fields
        west, south, east, north = job.bbox_wgs84
        bbox_area = _compute_bbox_area_km2(west, south, east, north)

        if bbox_area >= 10000:
            raise ValueError(f"bbox_area_km2 ({bbox_area:.1f}) must be less than 10000 km²")

        n_months = _compute_n_months(job.start_month, job.end_month)

        # Auto-detect UTM zone from center of bbox
        center_lat = (south + north) / 2
        center_lng = (west + east) / 2
        epsg = _detect_utm_epsg(center_lat, center_lng)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, api_key, aoi_name,
                    bbox_west, bbox_south, bbox_east, bbox_north,
                    epsg, start_month, end_month, chunk_size,
                    n_months, bbox_area_km2
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "pending",
                    api_key,
                    job.aoi_name,
                    west,
                    south,
                    east,
                    north,
                    epsg,
                    job.start_month,
                    job.end_month,
                    job.chunk_size,
                    n_months,
                    bbox_area,
                ),
            )
            conn.commit()

        return job_id

    def claim(self, worker_id: str) -> JobDetail | None:
        """Atomically claim the oldest pending job.

        Uses UPDATE ... WHERE id = (SELECT ...) RETURNING * for atomic claim.

        Args:
            worker_id: Identifier of the claiming worker

        Returns:
            JobDetail if a job was claimed, None if no pending jobs
        """
        import datetime

        claimed_at = datetime.datetime.now().isoformat()

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = 'claimed', claimed_at = ?, worker_id = ?
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT 1
                )
                RETURNING *
                """,
                (claimed_at, worker_id),
            )

            row = cursor.fetchone()
            conn.commit()

            if row is None:
                return None

            return JobDetail(**dict(row))

    def update_status(
        self, job_id: str, status: str, *, error: str | None = None, store_path: str | None = None
    ) -> bool:
        """Update job status and optional fields.

        Args:
            job_id: The job ID to update
            status: New status (running, done, failed, etc.)
            error: Optional error message for failed jobs
            store_path: Optional path to stored results

        Returns:
            True if the job was found and updated, False otherwise
        """
        import datetime

        fields = ["status = ?"]
        params: list[str | None] = [status]

        if status in ("done", "failed"):
            fields.append("completed_at = ?")
            params.append(datetime.datetime.now().isoformat())

        if error is not None:
            fields.append("error = ?")
            params.append(error)

        if store_path is not None:
            fields.append("store_path = ?")
            params.append(store_path)

        params.append(job_id)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def get(self, job_id: str) -> JobDetail | None:
        """Get full details of a job by ID.

        Args:
            job_id: The job ID to look up

        Returns:
            JobDetail if found, None otherwise
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            )
            row = cursor.fetchone()

            if row is None:
                return None

            return JobDetail(**dict(row))

    def list_for_key(self, api_key: str) -> list[JobStatus]:
        """List all jobs submitted by a specific API key.

        Args:
            api_key: The API key to filter by

        Returns:
            List of JobStatus summaries
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row

            cursor = conn.execute(
                """
                SELECT id, status, aoi_name, created_at, completed_at, error
                FROM jobs
                WHERE api_key = ?
                ORDER BY created_at DESC
                """,
                (api_key,),
            )
            rows = cursor.fetchall()

            return [JobStatus(**dict(row)) for row in rows]
