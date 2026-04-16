"""Benchmark harness: measure storage, access cost, and reconstruction quality.

All results are written to a DuckDB database for easy querying.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import numpy as np

from spacetime.access import AccessResult

logger = logging.getLogger(__name__)


def init_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Initialize the benchmark database with required tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS storage_metrics (
            aoi TEXT,
            representation TEXT,
            variant TEXT,
            keyframe_interval INTEGER,
            total_bytes BIGINT,
            n_months INTEGER,
            n_chunks INTEGER,
            n_products INTEGER,
            bytes_per_month_per_chunk DOUBLE,
            compression_ratio DOUBLE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_metrics (
            aoi TEXT,
            representation TEXT,
            variant TEXT,
            pattern TEXT,
            month TEXT,
            product TEXT,
            bytes_fetched BIGINT,
            decode_time_ms DOUBLE,
            n_chunks_fetched INTEGER,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_metrics (
            aoi TEXT,
            representation TEXT,
            variant TEXT,
            month TEXT,
            chunk_id TEXT,
            psnr DOUBLE,
            ssim DOUBLE,
            max_abs_error INTEGER,
            mean_abs_error DOUBLE,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS delta_stats (
            aoi TEXT,
            keyframe_interval INTEGER,
            month TEXT,
            mean_abs_delta DOUBLE,
            max_abs_delta INTEGER,
            pct_zero DOUBLE,
            pct_under_10 DOUBLE,
            pct_under_50 DOUBLE,
            created_at TIMESTAMP DEFAULT current_timestamp
        )
    """)

    return conn


def record_storage(
    conn: duckdb.DuckDBPyConnection,
    aoi: str,
    representation: str,
    metrics: dict,
    raw_bytes_per_month_per_chunk: float | None = None,
) -> None:
    """Record storage metrics for a representation."""
    total = metrics["total_bytes"]
    n_months = metrics.get("n_months", 1)
    n_chunks = metrics.get("n_chunks", 1)
    n_products = metrics.get("n_products", 1)
    bytes_per = total / max(n_months * n_chunks, 1)

    # Compression ratio: raw uncompressed / stored
    if raw_bytes_per_month_per_chunk is not None:
        raw_total = raw_bytes_per_month_per_chunk * n_months * n_chunks
        ratio = raw_total / max(total, 1)
    else:
        ratio = None

    conn.execute(
        """INSERT INTO storage_metrics
           (aoi, representation, variant, keyframe_interval,
            total_bytes, n_months, n_chunks, n_products,
            bytes_per_month_per_chunk, compression_ratio, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            aoi,
            representation,
            metrics.get("variant", ""),
            metrics.get("keyframe_interval", 0),
            total,
            n_months,
            n_chunks,
            n_products,
            bytes_per,
            ratio,
            "",
        ],
    )


def record_access(
    conn: duckdb.DuckDBPyConnection,
    aoi: str,
    representation: str,
    variant: str,
    result: AccessResult,
) -> None:
    """Record an access simulation result."""
    conn.execute(
        """INSERT INTO access_metrics
           (aoi, representation, variant, pattern, month, product,
            bytes_fetched, decode_time_ms, n_chunks_fetched)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            aoi,
            representation,
            variant,
            result.pattern,
            result.month,
            result.product,
            result.bytes_fetched,
            result.decode_time_ms,
            result.n_chunks_fetched,
        ],
    )


def record_quality(
    conn: duckdb.DuckDBPyConnection,
    aoi: str,
    representation: str,
    variant: str,
    month: str,
    chunk_id: str,
    original: np.ndarray,
    reconstructed: np.ndarray,
) -> None:
    """Compute and record quality metrics for a reconstruction."""
    # Both should be uint16
    orig_f = original.astype(np.float64)
    recon_f = reconstructed.astype(np.float64)

    diff = orig_f - recon_f
    max_abs = int(np.max(np.abs(diff)))
    mean_abs = float(np.mean(np.abs(diff)))

    # PSNR
    mse = np.mean(diff**2)
    if mse == 0:
        psnr = float("inf")
    else:
        max_val = 10000.0  # typical max reflectance value
        psnr = 10 * np.log10(max_val**2 / mse)

    # SSIM (simplified, per-band average)
    ssim_val = _simple_ssim(original, reconstructed)

    conn.execute(
        """INSERT INTO quality_metrics
           (aoi, representation, variant, month, chunk_id,
            psnr, ssim, max_abs_error, mean_abs_error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [aoi, representation, variant, month, chunk_id, psnr, ssim_val, max_abs, mean_abs],
    )


def record_delta_stats(
    conn: duckdb.DuckDBPyConnection,
    aoi: str,
    keyframe_interval: int,
    stats: list[dict],
) -> None:
    """Record delta statistics from experimental encoding."""
    for s in stats:
        conn.execute(
            """INSERT INTO delta_stats
               (aoi, keyframe_interval, month,
                mean_abs_delta, max_abs_delta, pct_zero, pct_under_10, pct_under_50)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                aoi,
                keyframe_interval,
                s["month"],
                s["mean_abs_delta"],
                s["max_abs_delta"],
                s["pct_zero"],
                s["pct_under_10"],
                s["pct_under_50"],
            ],
        )


def print_storage_summary(conn: duckdb.DuckDBPyConnection) -> None:
    """Print a formatted storage comparison."""
    result = conn.execute("""
        SELECT aoi, representation, variant, keyframe_interval,
               total_bytes, n_months, n_chunks,
               bytes_per_month_per_chunk,
               compression_ratio
        FROM storage_metrics
        ORDER BY aoi, total_bytes
    """).fetchall()

    print("\n=== Storage Summary ===")
    print(
        f"{'AOI':<20} {'Rep':<15} {'Variant':<12} {'KF Int':>6} "
        f"{'Total MB':>10} {'Per Chunk/Mo':>12} {'Ratio':>8}"
    )
    print("-" * 90)
    for row in result:
        aoi, rep, var, kf, total, _nm, _nc, bpcm, ratio = row
        ratio_str = f"{ratio:.1f}x" if ratio else "N/A"
        print(
            f"{aoi:<20} {rep:<15} {var:<12} {kf:>6} "
            f"{total / 1e6:>10.2f} {bpcm:>12.0f} {ratio_str:>8}"
        )


def _simple_ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Simplified SSIM over all bands. Not windowed — just global stats."""
    a_f = a.astype(np.float64)
    b_f = b.astype(np.float64)

    mu_a = a_f.mean()
    mu_b = b_f.mean()
    sig_a = a_f.std()
    sig_b = b_f.std()
    sig_ab = np.mean((a_f - mu_a) * (b_f - mu_b))

    c1 = (0.01 * 10000) ** 2
    c2 = (0.03 * 10000) ** 2

    ssim = ((2 * mu_a * mu_b + c1) * (2 * sig_ab + c2)) / (
        (mu_a**2 + mu_b**2 + c1) * (sig_a**2 + sig_b**2 + c2)
    )
    return float(ssim)
