"""Build from existing geocoded SWOT amplitudes; never modify source rasters."""

import hashlib
import json
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr

import chronozarr

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    Path.home() / "projects/swot-slc-geocode/output/site_scouting/wax_lake_atchafalaya/geocoded"
)
OUT = ROOT / "data/stores/swot_intensity/local-20261002"
DATES = ["2024-12-11", "2025-05-06"]


def main():
    frames, masks, records = [], [], []
    reference = None
    for date, condition in zip(DATES, ["low", "high"], strict=True):
        path = SOURCE / f"wax_lake_{condition}_{date.replace('-', '')}_60m_amplitude_crop.tif"
        with rasterio.open(path) as src:
            assert src.tags()["product"] == "amplitude"
            grid = (src.shape, src.crs, src.transform)
            if reference is None:
                reference = grid
            assert grid == reference
            amplitude = src.read(1)
            assert amplitude.dtype == np.float32
            source_valid = (src.read_masks(1) != 0) & np.isfinite(amplitude)
            valid = source_valid & (amplitude > 0)
            # Arithmetic is explicit float32; log excludes zeros, negatives and nodata.
            intensity = amplitude * amplitude
            db = np.full_like(amplitude, np.nan)
            np.log10(intensity, out=db, where=valid)
            db *= np.float32(10)
            frame = np.stack([db, intensity, amplitude])
            frames.append(frame)
            masks.append(valid.astype("uint8"))
            with rasterio.open(SOURCE / path.name.replace("60m", "5m")) as companion:
                source_product = companion.tags()["source"]
            records.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "source_slc_from_companion_tags": source_product,
                    "date_from_filename": date,
                    "geocoded_tags": src.tags(),
                    "valid_pixels": int(valid.sum()),
                    "nonpositive_source_pixels_excluded": int(
                        (source_valid & (amplitude <= 0)).sum()
                    ),
                    "data_sha256": hashlib.sha256(frame.tobytes()).hexdigest(),
                    "mask_sha256": hashlib.sha256(valid.astype("uint8").tobytes()).hexdigest(),
                }
            )
    _shape, crs, transform = reference
    values = np.stack(frames)
    names = ["intensity_dB", "intensity", "amplitude"]
    bands = [
        {"name": name, "units": unit}
        for name, unit in zip(names, ["dB", "arbitrary power", "arbitrary amplitude"], strict=True)
    ]
    data = xr.DataArray(
        values,
        dims=("time", "band", "y", "x"),
        coords={"time": np.array(DATES, dtype="datetime64[ns]"), "band": names},
    )
    if not OUT.exists():
        chronozarr.encode(
            data,
            OUT,
            crs=str(crs),
            transform=list(transform)[:6],
            bands=bands,
            mask=np.stack(masks),
            n_lods=3,
            provenance={
                "sources": [r["path"] for r in records],
                "composite": "none",
                "gap_fill": "none",
                "notes": "Existing swot-slc-geocode amplitude outputs. Derived after geocoding: "
                "intensity=amplitude^2; dB=10*log10(intensity). Not calibrated sigma0. "
                "Mask excludes nonpositive amplitude. No additional warp or smoothing.",
            },
        )
    store = chronozarr.open_store(OUT)
    for i in range(2):
        assert np.array_equal(store.read(i).view("uint32"), values[i].view("uint32"))
        assert np.array_equal(store.read_mask(i), masks[i])
    report = {
        "store": str(OUT),
        "sources": records,
        "shape": list(values.shape),
        "crs": str(crs),
        "fixed_display_range_dB": [30, 80],
        "checks": {"level0_all_bands_bit_exact": True, "masks_exact": True},
        "limitations": [
            "Intensity derived after amplitude geocoding, not a fresh intensity geocoding "
            "run. Uncalibrated, not sigma0. No flood-change inference."
        ],
        "bytes": sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()),
    }
    (ROOT / "data/reports/swot-intensity-20261002.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
