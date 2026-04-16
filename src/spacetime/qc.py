"""Quality control: visual comparison and difference maps."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def reconstruction_diff_map(original: np.ndarray, reconstructed: np.ndarray) -> np.ndarray:
    """Compute per-band absolute difference.

    Args:
        original: uint16 (n_bands, H, W)
        reconstructed: uint16 (n_bands, H, W)

    Returns:
        uint16 (n_bands, H, W) — absolute differences
    """
    return np.abs(original.astype(np.int32) - reconstructed.astype(np.int32)).astype(np.uint16)


def save_comparison_panel(
    original_bands: np.ndarray,
    reconstructed_bands: np.ndarray,
    month: str,
    chunk_id: str,
    output_dir: Path,
) -> Path:
    """Save a 3-panel comparison: original, reconstructed, difference.

    Renders true color for original and reconstructed, and a heat map
    for the difference.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from spacetime.render import true_color

    orig_rgb = true_color(original_bands)
    recon_rgb = true_color(reconstructed_bands)
    diff = reconstruction_diff_map(original_bands, reconstructed_bands)
    diff_sum = diff.sum(axis=0).astype(np.float32)

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    axes[0].imshow(orig_rgb)
    axes[0].set_title(f"Original ({month})")
    axes[0].set_axis_off()

    axes[1].imshow(recon_rgb)
    axes[1].set_title(f"Reconstructed ({month})")
    axes[1].set_axis_off()

    im = axes[2].imshow(diff_sum, cmap="hot", vmin=0, vmax=np.percentile(diff_sum, 99))
    axes[2].set_title("Abs Diff (sum over bands)")
    axes[2].set_axis_off()
    plt.colorbar(im, ax=axes[2], shrink=0.8)

    fig.suptitle(f"Chunk {chunk_id} — {month}", fontsize=14)
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"compare_{chunk_id}_{month}.png"
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return out_path


def save_delta_stats_plot(
    delta_stats: list[dict],
    aoi: str,
    keyframe_interval: int,
    output_dir: Path,
) -> Path:
    """Plot delta statistics over time for chunk (0,0)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    months = [s["month"] for s in delta_stats]
    mean_abs = [s["mean_abs_delta"] for s in delta_stats]
    pct_zero = [s["pct_zero"] for s in delta_stats]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    ax1.bar(range(len(months)), mean_abs, color="steelblue")
    ax1.set_ylabel("Mean |delta|")
    ax1.set_title(f"{aoi} — Delta Statistics (kf_interval={keyframe_interval})")

    ax2.bar(range(len(months)), pct_zero, color="forestgreen")
    ax2.set_ylabel("% zero deltas")
    ax2.set_xlabel("Month")
    ax2.set_xticks(range(len(months)))
    ax2.set_xticklabels(months, rotation=45, ha="right")

    fig.tight_layout()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"delta_stats_{aoi}_kf{keyframe_interval}.png"
    fig.savefig(str(out_path), dpi=150, bbox_inches="tight")
    plt.close(fig)

    return out_path
