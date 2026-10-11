# Explore a store in a notebook

```python
chronozarr.view("my_store")
```

Install `chronozarr[notebook]`. For a local store, `view` starts a server on `127.0.0.1` that answers byte-range requests. It then shows the hosted viewer in an iframe. The viewer loads from the internet. The store is read from the machine that runs the kernel.

This works in local Jupyter and VS Code sessions. The browser must reach `127.0.0.1` on the machine that runs the kernel. Chrome asks for permission before a public page reaches a local address. Allow it, or use the "open in a tab" link under the frame. Safari can block an https page from loading `http://127.0.0.1`. Use Chrome or Firefox, or a self-hosted viewer.

If the frame stays empty, run `chronozarr.diagnose_view("my_store")`. It prints how many requests the browser has sent to the server and what to try next.

## Remote notebooks

The server listens on `127.0.0.1` of the machine that runs the kernel, and it never listens on another address. On JupyterHub, over SSH or in a container, your browser runs elsewhere. Pick the route that matches your setup.

| Setup | Route | Status |
|-------|-------|--------|
| Local Jupyter or VS Code | Default, nothing to set | Works |
| JupyterHub with `jupyter-server-proxy` | `viewer_dir="viewer"` (see below) | Works (checked with a stand-in proxy only) |
| Another proxy or tunnel that you control | `base_url="https://.../"` | Works |
| SSH port forward | `port=8765`, then `ssh -L 8765:127.0.0.1:8765 host` | Works with the same port number on both ends |
| VS Code Remote, dev containers | Port forwarding of the same port number | Documented only |

The proxy behind `base_url` must map that address to `http://127.0.0.1:<port>/`, remove its own prefix and pass `Range` headers. The stand-in proxy is described in [evidence.md](evidence.md#local-preview-and-remote-notebooks).

### JupyterHub and jupyter-server-proxy

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

`view` and `player` take the initial view: `view("my_store", product="ndvi")`, `player(url, product="band", band="B8", range=[0, 4000])`. See [Band roles and the first view](python.md#band-roles-and-the-first-view) for what these mean.
