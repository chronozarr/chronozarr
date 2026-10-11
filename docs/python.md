# Python API

The `chronozarr` package writes a store, reads it and opens it in xarray. The [getting started page](../README.md) has a first run. This page lists the functions. The docstring of `chronozarr.encode` lists every option of `encode`.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. For a project that imports the library, add it to that project's environment with
`uv add chronozarr` (or `uv add 'chronozarr[geo]'` for GeoTIFF and export
features), then run scripts with `uv run python script.py`. With pip, install
into the active environment using `pip install chronozarr`, then run
`python script.py` and `chronozarr` directly. If you need only the CLI, use
`uv tool install chronozarr` or a one-off
`uvx --from chronozarr chronozarr --help`; those isolated commands do not
install the library into your project.
The shell examples below assume the CLI is installed with `uv tool install`.

## Write a store

```python
import chronozarr

chronozarr.encode(da, "my_store", crs="EPSG:32631")
```

`da` is an xarray `DataArray` with dims `(time, band, y, x)`. Its dtype is uint8, uint16, int16 or float32. Its `x` and `y` coordinates are the projected pixel centres of an EPSG grid with north up.

The output directory must not exist, or it must be empty. The store is unsharded unless you pass `shard=True`.

## Read a store

```python
store = chronozarr.open_store("my_store")   # a local path or an http(s) URL
frame = store.read(t=0, lod=0)              # (band, y, x), the stored values
cell = store.read_cell(t=0, row=0, col=0, lod=0)
mask = store.read_mask(t=0, lod=0)          # None when the store has no mask
coverage = store.read_coverage(t=0, lod=0)  # None when the store has no coverage plane
physical = store.physical(t=0, lod=0)       # float32, scale and offset applied
da = store.to_xarray(lod=0, times=[0], physical=False)
```

`t` is the index of a timestep. `lod` is the pyramid level, and level 0 has the original resolution. `row` and `col` select a cell of the level.

`read`, `read_cell` and `to_xarray` return the stored values in the stored dtype. With `physical=True`, `to_xarray` returns float32 values, as `physical()` does. Each physical value is `stored * scale + offset` for its band. Invalid pixels are NaN.

`to_xarray` loads the requested level into memory.

## Open a store in xarray

```python
import xarray as xr

ds = xr.open_dataset("my_store", engine="chronozarr")
raw = xr.open_dataset("my_store", engine="chronozarr", physical=False)
```

The backend reads lazily. It returns physical values unless you pass `physical=False`. The `lod` argument selects the level, and the default is 0.

Install `chronozarr[dask]` to pass `chunks=` and get dask arrays.

A plain Zarr reader opens a level without chronozarr and returns the stored values. See [Read a store without chronozarr](how-it-works.md#read-a-store-without-chronozarr).

## Show a store

| Task | Page |
|------|------|
| Preview on your computer | [Preview a store](preview.md) |
| Send a temporary link | [Share a local store instantly](share.md) |
| Look at a store in Jupyter or VS Code | [Explore a store in a notebook](notebooks.md) |

## Band roles and the first view

Band names are free text. The viewer chooses the bands of True color, False color, NDVI, NDWI and Water by role: red, green, blue and nir. A band has a role when:

1. its `common_name` is the role (the STAC `eo:bands` vocabulary, [spec 4.4](../spec/CHRONOZARR.md)), or
2. it has no `common_name` and its name is the role (`red`, case ignored), or
3. it has no `common_name` and its name is the Sentinel-2 band for the role (`B04`, `B03`, `B02`, `B08`).

The first rule that matches decides. Nothing else is guessed: `band_1`, `NIR1` or `b4` have no role. A product whose roles are missing is not offered.

```sh
chronozarr bands my_store                                  # bands, roles, products
chronozarr bands my_store --band-role b4=red,b8=nir --dry-run
chronozarr bands my_store --band-role b4=red,b8=nir        # write them
```

`bands` lists each band with its common name, its role and the rule that gave it, and the products with what each one still needs. `--band-role NAME=ROLE` sets `common_name` on a local store, in place. `ROLE` is a STAC common name, or `none` to remove one. The command changes the root `zarr.json` and its consolidated metadata and nothing else. Data, `scale`, `offset` and `units` stay as they are. It refuses an unknown band, a name outside the vocabulary, and an assignment that leaves two bands with the same role. For an immutable published store, assign roles before the first upload. A later role change needs a new prefix, or a deliberate metadata update and cache purge; `publish --update` is only for appended timesteps.

`chronozarr encode` and `chronozarr convert` take `--band-role` too, so a store can carry the roles from the start. In Python, `convert` has `band_roles={"b8": "nir"}` and `encode` takes `Band(..., common_name="nir")` in `bands=`. A manifest that names its bands has no common names unless you give them.

The initial product and display limits are presentation. They are not stored in the store, so every other client still reads the same store. They travel in the viewer URL and in the notebook:

| Parameter | `player` and `view` argument | Meaning |
|---|---|---|
| `t` | `t` | Timestep index |
| `p` | `product` | `true_color`, `false_color`, `ndvi`, `ndwi`, `water` or `band` |
| `b` | `band` | Band of the single-band product |
| `r` | `range` | Display limits `low,high` of the single-band product, in physical units |

```sh
chronozarr link https://data.example.org/my_store --product band --band B8 --range 0,4000
# https://chronozarr.org/demo/?store=...&p=band&b=B8&r=0,4000
```

`link` checks each value against the store and prints the URL, because the viewer ignores a value that the store cannot honor. A product needs its roles. Display limits only apply to the single-band product of a band that is not reflectance-like: an unsigned 8-bit or 16-bit band whose largest possible physical value is 10 or less is toned with fixed reflectance limits, and every other band has adjustable ones. `from chronozarr.view import viewer_url` builds the same URL from Python. The viewer keeps the limits you set in its address bar, so copying the address shares them.

`chronozarr.add_chronozarr` adds a store as a layer on a leafmap or geemap MapLibre map. Install the `leafmap` or `geemap` extra. See [examples/leafmap](../examples/leafmap/README.md).
