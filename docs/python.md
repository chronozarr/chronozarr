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

A tunnel gives your local server an https address, so someone on another machine can open the store in the hosted viewer. The viewer needs an https store URL. A LAN address such as `http://192.168.1.5:8000` is blocked as mixed content.

1. Start the server on a fixed port. The command fails when the port is taken, and it prints the link only after the server answers for your store.

   ```sh
   chronozarr preview my_store --port 8765 --no-open
   ```

2. Check the store locally before you start the tunnel.

   ```sh
   curl -s http://127.0.0.1:8765/my_store/zarr.json | head -c 100
   ```

3. Point the tunnel at the port. For example, with cloudflared:

   ```sh
   cloudflared tunnel --url http://127.0.0.1:8765
   ```

4. Run doctor on the tunnel URL.

   ```sh
   chronozarr doctor https://<random>.trycloudflare.com/my_store
   ```

5. Stop the preview with Ctrl-C. Run it again with the tunnel address to print the link to share.

   ```sh
   chronozarr preview my_store --port 8765 --no-open --base-url https://<random>.trycloudflare.com
   ```

A tunnel targets a port, not a process. If another program owns the port, the tunnel exposes that program. Step 1 refuses a taken port for this reason. Do not start a tunnel on a port that you have not checked.

Anyone with the link can read the store while the tunnel runs. Every read reaches your machine, so throughput is your upload bandwidth. The link stops working when you stop the command or the tunnel. For a link that lasts, upload the store. See [hosting.md](hosting.md).

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

`chronozarr.add_chronozarr` adds a store as a layer on a leafmap or geemap MapLibre map. Install the `leafmap` or `geemap` extra. See [examples/leafmap](../examples/leafmap/README.md).
