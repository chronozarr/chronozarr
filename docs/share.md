# Share a local store instantly

```sh
chronozarr share my_store
```

The command starts a local server and a temporary Cloudflare tunnel. It checks that the public route works, then opens a viewer link. The person who gets the link needs a modern browser. They need no Python, no chronozarr install and no cloud storage account.

## Which command do I use?

| I want to | Use |
|-----------|-----|
| Look at a store on my computer | [`chronozarr preview`](preview.md) |
| Send someone a temporary link | `chronozarr share` |
| Make a link that lasts | [`chronozarr publish`](hosting.md#chronozarr-publish) |
| Show a store in my own web page | [Embed the viewer](embedding.md) |
| Keep the data private | [Private stores](private.md) |

## How it works

1. `share` serves the store from your laptop on `127.0.0.1`.
2. `cloudflared` gives that server a public https address.
3. `share` runs `chronozarr doctor` on the public address, then prints the viewer link.

The viewer page comes from chronozarr.org. The data does not. The viewer reads the store through the tunnel, directly from your machine. chronozarr.org holds no copy of your dataset.

The link works while the command runs. Each read uses your upload bandwidth, so more viewers or faster scrubbing cost more. Anyone with the link can read the store. For a link that lasts, or one that needs a login, use [`publish`](hosting.md#chronozarr-publish) or [private hosting](private.md).

## Before you start

Install these once:

- Python 3.11 or later.
- `chronozarr[geo]`, which adds GeoTIFF support.
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) on your `PATH`. A quick tunnel needs no Cloudflare account or configuration.

```sh
python -m pip install --upgrade "chronozarr[geo]==0.4.0"
```

## From GeoTIFFs to a link

The input files must have one date in each name. Accepted forms include `YYYYMMDD`, `YYYY-MM-DD` and `YYYY-MM`. Every file must use the same north-up grid, CRS, dimensions, bands, dtype and band metadata. A directory is not searched recursively. Quote a glob, such as `"rasters/**/*.tif"`, to include subdirectories. `convert` checks every file before it writes the store.

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

4. Preview the store on your laptop. Press `Ctrl-C` when the local check is complete.

   ```sh
   chronozarr preview my_store
   ```

5. Start sharing.

   ```sh
   chronozarr share my_store
   ```

   Keep this terminal open while your colleague uses the link. A new hostname can take time to become reachable. The command waits up to 90 seconds, and that wait does not guarantee that public DNS propagates in time.

6. Choose the view in the browser.

   Select a product or band, move to a timestep, zoom or pan, and click the map to inspect values. Click `Copy link` to copy the current store and view: the timestep, product, band or display limits, zoom and map centre. The link does not store playback or loop settings. It copies a redacted store URL, so a signed query is not carried into the link.

7. Send the copied link to your colleague.

   They can watch, scrub the timeline, zoom, pan and inspect pixels while the command runs.

8. Stop sharing with `Ctrl-C` in the `chronozarr share` terminal. This stops the tunnel and the local server.

## Options

`--port`, `--viewer`, `--viewer-dir` and `--no-open` mean the same as for [`preview`](preview.md#options). Use `--no-open` to open the link yourself:

```sh
chronozarr share my_store --no-open
```

## Safety

A tunnel targets a port, not a process. If another program owns the port, the tunnel exposes that program. `share` refuses a port that is taken. Do not start a tunnel by hand on a port that you have not checked.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `cloudflared` not found | Install it and make sure it is on your `PATH` |
| The command waits, then reports the route is not reachable | The new hostname has not propagated. Run the command again |
| The link opens but the map stays empty | Run `chronozarr doctor` on the store. See [hosting.md](hosting.md#1-checklist) |
| Scrubbing is slow | Every read passes through your upload link. Use [`publish`](hosting.md#chronozarr-publish) for more than a few viewers |
