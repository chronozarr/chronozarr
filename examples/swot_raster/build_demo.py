"""Build a small, lossless two-date float32 SWOT raster demo from existing NetCDFs.

Source files are read only. Select a common 512-pixel window on the native 100 m
grid, stage GeoTIFFs with explicit validity masks, then exercise convert, xarray
and export-cog. This is a software-fidelity test, not a hydrologic analysis.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from rasterio.windows import Window, from_bounds

import chronozarr
from chronozarr.convert import convert
from chronozarr.export import export_cog

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path.home() / "geodata/nisar_swot_water_detection",
    )
    parser.add_argument("--quality", choices=["all", "good", "usable"], default="all")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    prefix = "local" if args.quality == "all" else args.quality
    args.out = args.out or ROOT / f"data/stores/swot_roanoke/{prefix}-20261002"
    policy = {
        "all": "finite and not source fill; no quality filtering",
        "good": "finite and not source fill; wse_qual == 0 (good only)",
        "usable": "finite and not source fill; wse_qual <= 1 (good or suspect)",
    }[args.quality]
    pattern = "roanoke*/*.nc"
    paths = sorted(args.source_root.glob(pattern))
    if len(paths) != 2:
        raise ValueError(
            f"Expected two Roanoke raster NetCDFs matching {args.source_root / pattern}, "
            f"found {len(paths)}. examples/swot_raster/README.md says where to get them."
        )
    staging = ROOT / f"data/examples/swot_roanoke_20261002_{args.quality}"
    staging.mkdir(parents=True, exist_ok=True)
    sources = [rasterio.open(f"netcdf:{path}:wse") for path in paths]
    try:
        assert all(source.crs == sources[0].crs for source in sources)
        assert all(source.dtypes == ("float32",) for source in sources)
        assert all(source.transform.a == 100 and source.transform.e == -100 for source in sources)
        west = max(s.bounds.left for s in sources)
        south = max(s.bounds.bottom for s in sources)
        east = min(s.bounds.right for s in sources)
        north = min(s.bounds.top for s in sources)
        windows = [from_bounds(west, south, east, north, s.transform) for s in sources]
        for window in windows:
            assert all(abs(v - round(v)) < 1e-6 for v in tuple(window.flatten()))
        windows = [window.round_offsets().round_lengths() for window in windows]
        arrays = [
            source.read(1, window=window) for source, window in zip(sources, windows, strict=True)
        ]
        quality = []
        for path, window, source in zip(paths, windows, sources, strict=True):
            with rasterio.open(f"netcdf:{path}:wse_qual") as q:
                assert q.transform == source.transform and q.crs == source.crs
                quality.append(q.read(1, window=window))
        valid = [np.isfinite(a) & (a != s.nodata) for a, s in zip(arrays, sources, strict=True)]
        joint = valid[0] & valid[1]
        size = 512
        candidates = [
            (int(joint[row : row + size, col : col + size].sum()), row, col)
            for row in range(0, joint.shape[0] - size + 1, 64)
            for col in range(0, joint.shape[1] - size + 1, 64)
        ]
        count, row, col = max(candidates)
        if count == 0:
            raise ValueError("No common valid WSE observations in the native-grid overlap")
        crops = [a[row : row + size, col : col + size] for a in arrays]
        masks = [a[row : row + size, col : col + size] for a in valid]
        raw_masks = [mask.copy() for mask in masks]
        quality_crops = [q[row : row + size, col : col + size] for q in quality]
        if args.quality != "all":
            limit = 0 if args.quality == "good" else 1
            masks = [mask & (q <= limit) for mask, q in zip(masks, quality_crops, strict=True)]
        row0, col0 = windows[0].row_off + row, windows[0].col_off + col
        transform = sources[0].window_transform(
            Window.from_slices((row0, row0 + size), (col0, col0 + size))
        )
        items, records = [], []
        for path, source, crop, mask, raw_mask, q in zip(
            paths, sources, crops, masks, raw_masks, quality_crops, strict=True
        ):
            time = source.tags()["NC_GLOBAL#time_coverage_start"]
            target = staging / f"wse-{time[:10]}.tif"
            with (
                rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),
                rasterio.open(
                    target,
                    "w",
                    driver="GTiff",
                    width=size,
                    height=size,
                    count=1,
                    dtype="float32",
                    crs=source.crs,
                    transform=transform,
                    tiled=True,
                    compress="deflate",
                    blockxsize=256,
                    blockysize=256,
                ) as dst,
            ):
                dst.write(crop, 1)
                dst.write_mask(mask.astype("uint8") * 255)
                dst.set_band_description(1, "wse")
                dst.set_band_unit(1, "m")
                dst.update_tags(
                    SOURCE=path.name,
                    VARIABLE="wse",
                    VALIDITY=policy,
                )
            items.append({"uri": str(target), "datetime": time})
            records.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "time": time,
                    "source_fill": source.nodata,
                    "valid_pixels": int(mask.sum()),
                    "raw_valid_pixels": int(raw_mask.sum()),
                    "quality_counts": {
                        name: int((raw_mask & (q == flag)).sum())
                        for flag, name in enumerate(["good", "suspect", "degraded", "bad"])
                    },
                    "negative_valid_pixels": int(((crop < 0) & mask).sum()),
                    "zero_valid_pixels": int(((crop == 0) & mask).sum()),
                    "valid_range_m": [float(crop[mask].min()), float(crop[mask].max())],
                    "data_sha256": hashlib.sha256(crop.tobytes()).hexdigest(),
                    "mask_sha256": hashlib.sha256(mask.astype("uint8").tobytes()).hexdigest(),
                }
            )
        manifest = staging / "manifest.json"
        manifest.write_text(json.dumps({"bands": ["wse"], "items": items}, indent=2))
        if not args.out.exists():
            convert(
                manifest,
                args.out,
                shard=False,
                n_lods=2,
                resume=True,
                provenance={
                    "sources": [p.name for p in paths],
                    "composite": "none",
                    "gap_fill": "none",
                    "notes": (
                        f"Native-grid crop only. Raw WSE retained. Mask: {policy}. "
                        "No interpolation."
                    ),
                },
            )
        store = chronozarr.open_store(args.out)
        checks = {}
        for i, (crop, mask) in enumerate(zip(crops, masks, strict=True)):
            assert np.array_equal(store.read(i)[0].view("uint32"), crop.view("uint32"))
            stored_mask = store.read_mask(i)
            assert stored_mask is not None, f"store has no mask plane at t={i}"
            assert np.array_equal(stored_mask.astype(bool), mask)
        checks["level0_data_and_masks_bit_exact"] = True
        ds = store.to_xarray()
        assert ds.dtype == np.float32
        assert ds.attrs["units"] == "m"
        assert ds.band_units.values.tolist() == ["m"]
        assert store.attrs.bands[0].units == "m"
        assert np.array_equal(ds.values.view("uint32"), np.stack(crops)[:, None].view("uint32"))
        assert np.array_equal(ds.coords["mask"].values.astype(bool), np.stack(masks))
        checks["xarray_values_dtype_masks"] = True
        checks["store_band_units_m"] = True
        with xr.open_dataset(args.out, engine="chronozarr", physical=False) as lazy:
            assert lazy[store.attrs.variable].attrs["units"] == "m"
            assert np.array_equal(
                lazy[store.attrs.variable].values.view("uint32"),
                np.stack(crops)[:, None].view("uint32"),
            )
            assert np.array_equal(
                lazy[store.attrs.mask_variable].values.astype(bool), np.stack(masks)
            )
        with xr.open_dataset(args.out, engine="chronozarr") as physical:
            values = physical[store.attrs.variable].values[:, 0]
            assert np.isnan(values[~np.stack(masks)]).all()
            assert np.array_equal(values[np.stack(masks)], np.stack(crops)[np.stack(masks)])
        checks["xarray_backend_raw_physical_masks"] = True
        for i, (crop, mask) in enumerate(zip(crops, masks, strict=True)):
            export_dir = staging / "export"
            existing = list(export_dir.glob(f"L0_{store.attrs.times[i][:10]}*.tif"))
            target = existing[0] if existing else export_cog(args.out, export_dir, times=[i])[0]
            with rasterio.open(target) as exported:
                assert np.array_equal(
                    exported.read(1)[mask].view("uint32"), crop[mask].view("uint32")
                )
                assert np.array_equal(exported.read_masks(1) > 0, mask)
                assert exported.crs == sources[0].crs and exported.transform == transform
                assert exported.units == ("m",)
        checks["cog_valid_values_masks_units_grid"] = True
        report = {
            "store": str(args.out),
            "shape": [2, 1, size, size],
            "dtype": "float32",
            "units": "m",
            "crs": str(sources[0].crs),
            "transform": list(transform)[:6],
            "sources": records,
            "raw_shared_valid_pixels": count,
            "shared_valid_pixels": int((masks[0] & masks[1]).sum()),
            "checks": checks,
            "bytes": sum(p.stat().st_size for p in args.out.rglob("*") if p.is_file()),
            "quality_policy": policy,
            "known_limitations": [
                "Two dates only; no valid exact-zero WSE pixel in this window.",
            ],
        }
        suffix = "" if args.quality == "all" else f"-{args.quality}"
        report_path = ROOT / f"data/reports/swot-roanoke-20261002{suffix}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        for source in sources:
            source.close()


if __name__ == "__main__":
    main()
