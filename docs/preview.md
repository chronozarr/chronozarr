# Preview a store on your computer

```sh
chronozarr preview my_store
```

The command serves the store on `127.0.0.1`, prints the address and opens the viewer in your browser. Ctrl-C stops the server. `encode` and `convert` print this command when they finish. It serves a local directory; it does not use your cloud credentials to proxy an `s3://` or other private bucket.

## Options

| Option | Effect |
|--------|--------|
| `--port N` | Listens on port `N`. The command fails if anything already listens on `N`. Without it, the system picks a free port |
| `--viewer-dir DIR` | Serves a self-hosted viewer folder from the same server. No internet access is needed |
| `--viewer URL` | Uses another viewer deployment |
| `--base-url URL` | Prints viewer links that use this absolute `http(s)` URL for the server, for a tunnel or a port forward |
| `--no-open` | Prints the link and does not open a browser |

By default the viewer loads from `https://chronozarr.org/demo/`. That needs internet access. The store data stays on your machine and goes straight from the server to your browser. For offline use, make a viewer folder (see [viewer-distribution.md](viewer-distribution.md)) and pass `--viewer-dir`.

The server answers byte ranges and CORS. It sends `Cache-Control: no-cache` and answers a revalidation with `304`, so a reload re-reads only the objects that changed. `chronozarr doctor` warns on `no-cache`. The warning does not apply to a preview, where you may re-encode the store in place.

To show the store to someone on another machine, see [Share a local store instantly](share.md).
