"""Generate synthetic data and actual widget renderers from an installed wheel."""

import importlib
import importlib.metadata
import json
from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr
from chronozarr import add_chronozarr, encode

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/reports/notebook-ci"
BASE = "http://127.0.0.1:8765"


def main():
    installed = Path(chronozarr.__file__).resolve()
    assert not installed.is_relative_to(ROOT), f"Expected installed wheel, got {installed}"
    OUT.mkdir(parents=True, exist_ok=True)
    y, x = np.indices((128, 128))
    first = (x + y - 128).astype("float32")
    data = np.stack([first, -first, first * np.float32(0.5)])[:, None]
    mask = np.ones((3, 128, 128), dtype="uint8")
    mask[:, :16, :16] = 0
    store = OUT / "store"
    if not store.exists():
        encode(
            xr.DataArray(
                data,
                dims=("time", "band", "y", "x"),
                coords={
                    "time": np.array(
                        ["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]"
                    ),
                    "band": ["wse"],
                },
            ),
            store,
            crs="EPSG:32618",
            transform=[100, 0, 300000, 0, -100, 4000000],
            bands=[{"name": "wse", "units": "m"}],
            mask=mask,
            encoding="none",
            chunk_size=128,
            n_lods=1,
        )
    url = f"{BASE}/data/reports/notebook-ci/store"
    versions = {"chronozarr": importlib.metadata.version("chronozarr")}
    for backend in ["leafmap", "geemap"]:
        module = importlib.import_module(f"{backend}.maplibregl")
        kwargs = (
            {"add_sidebar": False, "add_floating_sidebar": False} if backend == "leafmap" else {}
        )
        m = module.Map(
            style={"version": 8, "sources": {}, "layers": []},
            controls={},
            height="400px",
            **kwargs,
        )
        m.use_message_queue(False)
        add_chronozarr(
            m,
            url,
            name=f"{backend} fixture",
            product="band",
            range=[-128, 128],
            reader_url=f"{BASE}/js/maplibre/layer.js",
        )
        (OUT / f"{backend}.js").write_text(m._esm)
        (OUT / f"{backend}.json").write_text(
            json.dumps({"map_options": m.map_options, "calls": m.calls, "height": m.height})
        )
        versions[backend] = importlib.metadata.version(backend)
    w = chronozarr.player(
        url,
        viewer=f"{BASE}/js/tileripper/index.html",
        product="band",
        band="wse",
        range=[-128, 128],
    )
    try:
        source = w._esm
        (OUT / "player.js").write_text(source.read_text() if isinstance(source, Path) else source)
        (OUT / "player.json").write_text(json.dumps(w.get_state()))
    finally:
        w.close()
    (OUT / "versions.json").write_text(json.dumps(versions, indent=2))
    print("Installed wheel renderer fixtures:", versions)


if __name__ == "__main__":
    main()
