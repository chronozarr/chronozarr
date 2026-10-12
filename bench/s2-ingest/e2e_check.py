"""End-to-end check on real Planetary Computer data, about 20 reads per asset window: STAC
search -> COG reads -> whole grid and 512-row strips -> monthly GeoTIFFs -> coverage counts ->
encode in default, one and one-timestep time slabs (needs #99) -> store.

    uv run python bench/s2-ingest/e2e_check.py OUT_DIR

The box straddles the UTM 18/19 boundary at 72 W on EPSG:32719, so scenes from zone 18 are
warped. Valid-scene counts are compared with an SCL-only count computed with pyproj, which
must never be lower (a pipeline count can be lower where a band is 0, never higher).
"""

import hashlib
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer

sys.path.insert(0, "examples/sentinel2_pc")
import catalog
import ingest
import mosaic
import performance

import chronozarr
from chronozarr import _writer

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
BBOX, EPSG = (-72.06, -7.62, -71.96, -7.52), 32719  # straddles the UTM 18/19 boundary at 72 W
by_month = catalog.search_scenes_by_month(BBOX, "2024-07-01", "2024-08-31", max_cloud_pct=80.0)
report = {m: {"scenes": len(s), "epsgs": sorted({x.epsg for x in s})} for m, s in by_month.items()}
print("search:", report)
res = performance.detect_resources()
runs = {}
for name, kw in (
    ("whole", dict(settings=performance.plan_settings(res))),
    (
        "strips",
        dict(settings=performance.plan_settings(res, requests=4, max_requests=4), strip_rows=512),
    ),
):
    t = time.perf_counter()
    rep = mosaic.RunReport()
    outs = mosaic.build_monthly_mosaics(
        by_month, BBOX, EPSG, OUT / name, sign=catalog.sign_href, report=rep, **kw
    )
    runs[name] = {m: mosaic.load_mosaic(p) for m, p in outs.items()}
    print(
        name,
        f"{time.perf_counter() - t:.1f}s strips/month={rep.windows} reads={rep.reads} native={rep.reads_native} retries={rep.read_retries} failed={rep.scenes_failed}",
    )
for m in runs["whole"]:
    a, b = runs["whole"][m], runs["strips"][m]
    same = (
        np.array_equal(a["bands"], b["bands"])
        and np.array_equal(a["valid_count"], b["valid_count"])
        and a["bands_sha256"] == b["bands_sha256"]
        and a["carried_from"] == b["carried_from"]
    )
    print(m, "whole == strips (bands, counts, digest, chain):", same)

# Independent inclusion check: SCL-only count from pyproj-located 20 m pixels.
transform, H, W = mosaic.compute_target_grid(BBOX, EPSG)
jj, ii = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
gx, gy = transform.c + jj * transform.a, transform.f + ii * transform.e
for m, scenes in by_month.items():
    ref = np.zeros((H, W), np.int32)
    for s in scenes:
        with rasterio.open(catalog.sign_href(s.scl_href)) as src:
            x, y = Transformer.from_crs(EPSG, src.crs.to_epsg(), always_xy=True).transform(gx, gy)
            col = np.floor((x - src.transform.c) / src.transform.a).astype(int)
            row = np.floor((y - src.transform.f) / src.transform.e).astype(int)
            inside = (col >= 0) & (col < src.width) & (row >= 0) & (row < src.height)
            r0, r1, c0, c1 = (
                row[inside].min(),
                row[inside].max() + 1,
                col[inside].min(),
                col[inside].max() + 1,
            )
            scl = src.read(1, window=rasterio.windows.Window.from_slices((r0, r1), (c0, c1)))
        cls = np.zeros((H, W), np.uint8)
        cls[inside] = scl[row[inside] - r0, col[inside] - c0]
        ref += mosaic._SCL_LUT[cls]
    k = runs["whole"][m]["valid_count"].astype(np.int32)
    print(
        m,
        f"pixels with count > SCL-only reference: {int((k > ref).sum())}; count < reference (a band is 0): {int((k < ref).sum())} of {k.size}; mean count {k.mean():.2f}, max {k.max()}",
    )


# Encode: default slabs vs one slab vs 1-timestep slabs; tree hashes; coverage = counts.
def tree(root):
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


hashes = {}
for name, slab in (("default", _writer.SLAB_BYTES), ("one_slab", 10**15), ("one_step", 1)):
    _writer.SLAB_BYTES = slab
    store = OUT / f"store-{name}"
    ingest.encode(OUT / "whole", store)
    hashes[name] = tree(store)
print("store tree hashes:", hashes, "all equal:", len(set(hashes.values())) == 1)
st = chronozarr.open_store(OUT / "store-default")
for t, m in enumerate(sorted(runs["whole"])):
    cov = st.read_coverage(t)
    print(
        m,
        "store coverage == min(valid_count,255):",
        np.array_equal(cov, np.minimum(runs["whole"][m]["valid_count"], 255)),
        "store bands == month:",
        np.array_equal(st.read(t), runs["whole"][m]["bands"]),
    )
