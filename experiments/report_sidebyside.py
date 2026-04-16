"""Side-by-side comparison report across AOIs.

Usage:
    uv run python experiments/report_sidebyside.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "reports" / "bench.duckdb"


def main():
    if not DB_PATH.exists():
        print(f"No benchmark DB at {DB_PATH}. Run quick_bench.py first.")
        return

    db = duckdb.connect(str(DB_PATH), read_only=True)

    # Get AOIs that have data
    aois = [
        r[0]
        for r in db.execute("SELECT DISTINCT aoi FROM storage_metrics ORDER BY aoi").fetchall()
    ]

    if not aois:
        print("No data in benchmark DB.")
        return

    print("=" * 80)
    print("SIDE-BY-SIDE COMPARISON")
    print("=" * 80)

    # ===== H1: Product Unification =====
    print("\n--- H1: Product Unification ---")
    print(f"  {'AOI':<25} {'A (3 prods)':>14} {'B1 (multiband)':>16} {'Savings':>10}")
    print(f"  {'-' * 68}")
    for aoi in aois:
        a_row = db.execute(
            """
            SELECT total_bytes FROM storage_metrics
            WHERE aoi=? AND representation='baseline_a'
        """,
            [aoi],
        ).fetchone()
        b1_row = db.execute(
            """
            SELECT total_bytes FROM storage_metrics
            WHERE aoi=? AND variant='b1'
        """,
            [aoi],
        ).fetchone()
        if a_row and b1_row:
            a, b1 = a_row[0], b1_row[0]
            pct = (1 - b1 / max(a, 1)) * 100
            print(f"  {aoi:<25} {a / 1e6:>12.1f} MB {b1 / 1e6:>14.1f} MB {pct:>8.0f}%")

    # ===== H2: Temporal Encoding =====
    print("\n--- H2: Temporal Encoding (vs B2_chunked) ---")
    print(
        f"  {'AOI':<25} {'B2_chunked':>12} {'X_kf3':>10} {'X_kf6':>10} "
        f"{'X_kf12':>10} {'Best win':>10}"
    )
    print(f"  {'-' * 80}")
    for aoi in aois:
        b2c_row = db.execute(
            """
            SELECT total_bytes FROM storage_metrics
            WHERE aoi=? AND variant='b2_chunked'
        """,
            [aoi],
        ).fetchone()
        if not b2c_row:
            continue
        b2c = b2c_row[0]

        vals = {}
        for kf in [3, 6, 12]:
            row = db.execute(
                """
                SELECT total_bytes FROM storage_metrics
                WHERE aoi=? AND representation='experimental' AND keyframe_interval=?
            """,
                [aoi, kf],
            ).fetchone()
            if row:
                vals[kf] = row[0]

        best_pct = max((1 - v / max(b2c, 1)) * 100 for v in vals.values()) if vals else 0
        kf3_str = f"{vals.get(3, 0) / 1e6:.1f}" if 3 in vals else "N/A"
        kf6_str = f"{vals.get(6, 0) / 1e6:.1f}" if 6 in vals else "N/A"
        kf12_str = f"{vals.get(12, 0) / 1e6:.1f}" if 12 in vals else "N/A"
        print(
            f"  {aoi:<25} {b2c / 1e6:>10.1f}MB {kf3_str:>9}MB "
            f"{kf6_str:>9}MB {kf12_str:>9}MB {best_pct:>8.1f}%"
        )

    # ===== H3: Incremental Time-Step Cost =====
    inc_data = db.execute("""
        SELECT aoi, variant, is_keyframe_boundary,
               AVG(bytes_marginal) as avg_bytes,
               AVG(decode_time_ms) as avg_ms,
               COUNT(*) as n
        FROM incremental_cost
        GROUP BY aoi, variant, is_keyframe_boundary
        ORDER BY aoi, variant, is_keyframe_boundary
    """).fetchall()

    if inc_data:
        print("\n--- H3: Incremental Time-Step Cost ---")
        print(
            f"  {'AOI':<25} {'Variant':<15} {'Type':<12} {'Avg bytes':>12} {'Avg ms':>8} {'n':>4}"
        )
        print(f"  {'-' * 80}")
        for row in inc_data:
            aoi, var, is_kf, avg_b, avg_ms, n = row
            tag = "keyframe" if is_kf else "delta"
            print(f"  {aoi:<25} {var:<15} {tag:<12} {avg_b:>12,.0f} {avg_ms:>8.1f} {n:>4}")

    # ===== Change Analysis =====
    change_data = db.execute("""
        SELECT aoi,
               AVG(pct_changed) as avg_changed,
               AVG(mean_abs_delta) as avg_delta,
               AVG(delta_entropy) as avg_entropy,
               MIN(pct_changed) as min_changed,
               MAX(pct_changed) as max_changed
        FROM change_analysis
        GROUP BY aoi ORDER BY aoi
    """).fetchall()

    if change_data:
        print("\n--- Temporal Change Profile ---")
        print(
            f"  {'AOI':<25} {'Avg changed%':>14} {'Avg |delta|':>12} "
            f"{'Entropy ratio':>14} {'Range':>15}"
        )
        print(f"  {'-' * 84}")
        for row in change_data:
            aoi, avg_ch, avg_d, avg_e, min_ch, max_ch = row
            print(
                f"  {aoi:<25} {avg_ch:>13.1f}% {avg_d:>12.1f} "
                f"{avg_e:>14.3f} {min_ch:.0f}-{max_ch:.0f}%"
            )

    # ===== Quality =====
    q_data = db.execute("""
        SELECT aoi, variant, AVG(psnr), MAX(max_abs_error)
        FROM quality_metrics
        GROUP BY aoi, variant ORDER BY aoi, variant
    """).fetchall()

    if q_data:
        print("\n--- Reconstruction Quality ---")
        for aoi, var, psnr, worst in q_data:
            p = "inf (lossless)" if psnr == float("inf") else f"{psnr:.1f} dB"
            print(f"  {aoi:<25} {var:<20} PSNR={p}  worst_err={worst}")

    # ===== Summary Verdict =====
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    print("""
  H1 (Product Unification): ROBUST WIN (12-19%)
     Store multiband once, derive products late. Uncontroversial.

  H2 (Temporal Delta Encoding): LANDSCAPE-DEPENDENT, MODEST
     Sahara (low change): ~13% savings at kf=12
     Iowa (high change): ~1.5% savings — near zero
     Monthly composites are too temporally "new" in raw reflectance.

  H3 (Incremental Time-Step Cost): MODERATE (17-21%)
     Delta steps cost ~80% of a full tile. Not transformative.

  V2 (Brightness/Norm Decomposition): RULED OUT
     Decomposed representation is 1.35x LARGER when compressed.
     Monthly change is genuinely multi-dimensional.

  Architecture Value:
     The "spacetime chunk" idea lives in product unification and
     chunk-first access, not in temporal delta coding of raw
     monthly reflectance. The temporal axis needs a different
     representation domain or cadence to unlock large wins.
""")

    db.close()


if __name__ == "__main__":
    main()
