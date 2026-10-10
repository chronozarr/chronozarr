# Python API

The `chronozarr` package writes a store, reads it and opens it in xarray. The [README](../README.md) has a first run. This page lists the functions. The docstring of `chronozarr.encode` lists every option of `encode`.

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

A plain Zarr reader opens a level without chronozarr and returns the stored values. See [Read a store without chronozarr](../README.md#read-a-store-without-chronozarr).

## Preview from the command line

```sh
chronozarr preview my_store
```

The command serves the store on `127.0.0.1`, prints the address and opens the viewer in your browser. Ctrl-C stops the server. `encode` and `convert` print this command when they finish.

| Option | Effect |
|--------|--------|
| `--port N` | Listens on port `N`. The command fails if anything already listens on `N`. Without it, the system picks a free port |
| `--viewer-dir DIR` | Serves a self-hosted viewer folder from the same server. No internet access is needed |
| `--viewer URL` | Uses another viewer deployment |
| `--base-url URL` | Prints viewer links that use this absolute `http(s)` URL for the server, for a tunnel or a port forward |
| `--no-open` | Prints the link and does not open a browser |

By default the viewer loads from `https://chronozarr.org/demo/`. That needs internet access. The store data stays on your machine and goes straight from the server to your browser. For offline use, make a viewer folder (see [viewer-distribution.md](viewer-distribution.md)) and pass `--viewer-dir`.

The server answers byte ranges and CORS. It sends `Cache-Control: no-cache` and answers a revalidation with `304`, so a reload re-reads only the objects that changed. `chronozarr doctor` warns on `no-cache`. The warning does not apply to a preview, where you may re-encode the store in place.

### Share a store through a tunnel

This workflow takes a folder of dated GeoTIFFs to a viewer link for a colleague.

Install these prerequisites once:

- Python 3.11 or later.
- `chronozarr[geo]`, which adds GeoTIFF support.
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  on your `PATH`. A quick tunnel needs no Cloudflare account or configuration.

Install the Python package with:

```sh
python -m pip install "chronozarr[geo]"
```

The input files must have one date in each name. Accepted forms include `YYYYMMDD`, `YYYY-MM-DD`
and `YYYY-MM`. Every file must use the same north-up grid, CRS, dimensions, bands, dtype and
band metadata. A directory is not recursive. Quote a glob, such as `"rasters/**/*.tif"`, to
include subdirectories. `convert` checks every file before it writes the store.

1. Check the files without writing anything.

   ```sh
   chronozarr convert "rasters/*.tif" my_store --dry-run
   ```

2. Convert the series.

   ```sh
   chronozarr convert "rasters/*.tif" my_store
   ```

3. Validate the store.

   ```sh
   chronozarr validate my_store
   ```

4. Preview the store on your laptop.

   ```sh
   chronozarr preview my_store
   ```

   Keep this terminal open while you inspect the map. Press `Ctrl-C` when the local check is
   complete.

5. Start sharing from the laptop.

   ```sh
   chronozarr share my_store
   ```

   Keep this terminal open while your colleague uses the link. The command starts its own local
   server and quick tunnel, checks the public route, and opens the verified viewer link. New
   hostnames may take time to become reachable. The command waits up to 90 seconds; that wait
   does not guarantee that public DNS propagates within 90 seconds.

6. Choose the view in the browser.

   Select a product or band, move to a timestep, zoom or pan, and click the map to inspect values.
   Click `Copy link` to copy the current view. The link does not store playback or loop settings.

7. Send the copied link to your colleague.

   The colleague needs only a modern browser. They do not need Python or a chronozarr install.
   They can watch, scrub the timeline, zoom, pan and inspect pixels while the command runs.

8. Stop sharing when you finish.

   Press `Ctrl-C` in the `chronozarr share` terminal. This stops the tunnel and the local server.
   Anyone with the link can read the store while the command runs. Each read uses your laptop's
   upload bandwidth, so more viewers or faster scrubbing increases that cost. The address is
   temporary and stops working with the command. For a durable or access-controlled route, upload
   the store instead; see [hosting.md](hosting.md).

`--port`, `--viewer`, `--viewer-dir`, and `--no-open` have the same meanings as for `preview`.
Use `--no-open` when you want to open the link yourself:

```sh
chronozarr share my_store --no-open
```

## Show a store in a notebook

```python
chronozarr.view("my_store")
```

Install `chronozarr[notebook]`. For a local store, `view` starts a server on `127.0.0.1` that answers byte-range requests. It then shows the hosted viewer in an iframe. The viewer loads from the internet. The store is read from the machine that runs the kernel.

This works in local Jupyter and VS Code sessions. The browser must reach `127.0.0.1` on the machine that runs the kernel. Chrome asks for permission before a public page reaches a local address. Allow it, or use the "open in a tab" link under the frame. Safari can block an https page from loading `http://127.0.0.1`. Use Chrome or Firefox, or a self-hosted viewer.

If the frame stays empty, run `chronozarr.diagnose_view("my_store")`. It prints how many requests the browser has sent to the server and what to try next.

### Remote notebooks

The server listens on `127.0.0.1` of the machine that runs the kernel, and it never listens on another address. On JupyterHub, over SSH or in a container, your browser runs elsewhere. Pick the route that matches your setup.

| Setup | Route | Status |
|-------|-------|--------|
| Local Jupyter or VS Code | Default, nothing to set | Works |
| JupyterHub with `jupyter-server-proxy` | `viewer_dir="viewer"` (see below) | Works (checked with a stand-in proxy only) |
| Another proxy or tunnel that you control | `base_url="https://.../"` | Works |
| SSH port forward | `port=8765`, then `ssh -L 8765:127.0.0.1:8765 host` | Works with the same port number on both ends |
| VS Code Remote, dev containers | Port forwarding of the same port number | Documented only |

The proxy behind `base_url` must map that address to `http://127.0.0.1:<port>/`, remove its own prefix and pass `Range` headers. The stand-in proxy is described in [evidence.md](evidence.md#local-preview-and-remote-notebooks).

#### JupyterHub and jupyter-server-proxy

A hub login cookie belongs to the hub origin. The hosted viewer on `chronozarr.org` cannot send it, so the hub rejects its requests to a proxied store. A viewer served from the hub origin can. Serve a self-hosted viewer next to the store:

1. Write a viewer folder once, where Node.js is available. See [viewer-distribution.md](viewer-distribution.md).

   ```sh
   npx --package chronozarr chronozarr-viewer viewer
   ```

2. Pass the folder to `view` or `player`.

   ```python
   chronozarr.view("my_store", viewer_dir="viewer")
   ```

   When the kernel runs under JupyterHub, `view` sends the browser to `/user/<name>/proxy/<port>/` on the hub. The notebook login protects the viewer and the store. Install `jupyter-server-proxy` where the Jupyter server runs, and restart the server.

3. On a Jupyter server that is not under JupyterHub, pass the route yourself. For a server at the root of its host, the route is `/proxy/<port>`. Otherwise put the server base URL in front.

   ```python
   chronozarr.view("my_store", viewer_dir="viewer", port=8765, base_url="/proxy/8765")
   ```

`base_url` is an absolute `http(s)` URL or a path on the notebook origin that starts with `/`. It is the address at which the browser reaches the root of the store server. The store is at `<base_url>/<store name>`.

Do not expose the server with a public `base_url` unless you want anyone with the link to read the store.

`chronozarr.player` shows a viewer that Python can control. See [examples/anywidget](../examples/anywidget/README.md).

`view` and `player` take the initial view: `view("my_store", product="ndvi")`, `player(url, product="band", band="B8", range=[0, 4000])`. See the next section for what these mean.

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

`bands` lists each band with its common name, its role and the rule that gave it, and the products with what each one still needs. `--band-role NAME=ROLE` sets `common_name` on a local store, in place. `ROLE` is a STAC common name, or `none` to remove one. The command changes the root `zarr.json` and its consolidated metadata and nothing else. Data, `scale`, `offset` and `units` stay as they are. It refuses an unknown band, a name outside the vocabulary, and an assignment that leaves two bands with the same role. A hosted copy needs its root `zarr.json` uploaded again, and a cached copy purged.

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
