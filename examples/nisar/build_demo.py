"""Build a native-grid NISAR cached-extract demo; sources are read only."""

import hashlib
import json
from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path.home() / "geodata/nisar_swot_water_detection/pilot_20260904"
OUT = ROOT / "data/stores/nisar/local-20261002"
DATES = ["2026-06-22", "2026-08-21"]


def main():
    frames, masks, records = [], [], []
    reference = None
    for date in DATES:
        path = SOURCE / f"nisar_{date.replace('-', '')}_frequencyA.npz"
        with np.load(path, allow_pickle=False) as z:
            grid = [z[k].copy() for k in ["x", "y", "epsg"]]
            if reference is None:
                reference = grid
            else:
                assert all(np.array_equal(a, b) for a, b in zip(reference, grid, strict=True))
            raw = np.stack([z["HHHH"], z["HVHV"]])
            valid = (z["mask"] == 1) & np.isfinite(raw).all(axis=0) & (raw > 0).all(axis=0)
            db = np.full_like(raw, np.nan)
            np.log10(raw, out=db, where=valid[None])
            db *= np.float32(10)
            frame = np.concatenate([db, raw])
            frames.append(frame)
            masks.append(valid.astype("uint8"))
            records.append(
                {
                    "source": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "date_from_filename": date,
                    "valid_pixels": int(valid.sum()),
                    "data_sha256": hashlib.sha256(frame.tobytes()).hexdigest(),
                    "mask_sha256": hashlib.sha256(valid.astype("uint8").tobytes()).hexdigest(),
                }
            )
    x, y, epsg = reference
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    assert np.allclose(np.diff(x), dx) and np.allclose(np.diff(y), dy)
    names = ["HH_dB", "HV_dB", "HH_linear", "HV_linear"]
    bands = [{"name": name, "units": "dB" if i < 2 else "1"} for i, name in enumerate(names)]
    values = np.stack(frames)
    data = xr.DataArray(
        values,
        dims=("time", "band", "y", "x"),
        coords={"time": np.array(DATES, dtype="datetime64[ns]"), "band": names, "x": x, "y": y},
    )
    if not OUT.exists():
        chronozarr.encode(
            data,
            OUT,
            crs=f"EPSG:{int(epsg)}",
            transform=[dx, 0, float(x[0] - dx / 2), 0, dy, float(y[0] - dy / 2)],
            bands=bands,
            mask=np.stack(masks),
            encoding="none",
            n_lods=3,
            provenance={
                "sources": [r["source"] for r in records],
                "composite": "none",
                "gap_fill": "none",
                "notes": "Cached NPZ extracts. Dates from filenames; full acquisition metadata "
                "unavailable. Native grid. dB=10*log10(linear). Raw bands retained.",
            },
        )
    store = chronozarr.open_store(OUT)
    for i in range(2):
        assert np.array_equal(store.read(i).view("uint32"), values[i].view("uint32"))
        assert np.array_equal(store.read_mask(i), masks[i])
    assert store.to_xarray().band_units.values.tolist() == ["dB", "dB", "1", "1"]
    report = {
        "store": str(OUT),
        "sources": records,
        "crs": f"EPSG:{int(epsg)}",
        "shape": list(values.shape),
        "resolution": [dx, dy],
        "fixed_display_range_dB": [-25, 0],
        "checks": {
            "all_level0_bands_bit_exact": True,
            "masks_exact": True,
            "xarray_band_units": True,
        },
        "limitations": [
            "Cached extracts; original GCOV product IDs, exact timestamps and "
            "calibration metadata not recovered. No flood or change interpretation."
        ],
        "bytes": sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()),
    }
    target = ROOT / "data/reports/nisar-20261002.json"
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
