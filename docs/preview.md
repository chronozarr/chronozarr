# Preview a store on your computer

```sh
chronozarr preview my_store
```

The command serves a local store on `127.0.0.1` and opens it in your browser. Press Ctrl-C to stop the server. `encode` and `convert` print this command after `chronozarr share` when they finish. Preview serves a local directory; it cannot proxy an `s3://` or other private bucket using your cloud credentials.

Preview links, including those made with Copy link, open only on this computer. Use [`chronozarr share`](share.md) for a temporary public link that another person can open; it requires `cloudflared`.

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
