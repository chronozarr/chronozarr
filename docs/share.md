# Share a temporary link

```bash
chronozarr share my_store
```

The command makes a store on your computer reachable through a temporary public address, checks that a browser can read it there, and prints a viewer link. The person who gets the link needs a web browser. They need no Python, no chronozarr and no account.

## Temporary or permanent

| | Temporary link: `chronozarr share` | Permanent link: `chronozarr publish` |
|---|---|---|
| Where the data is | On your computer | In cloud storage that you control |
| How long the link works | While the command runs and your computer is on and online | As long as you keep the files |
| Who pays for downloads | Your upload connection | Your storage provider |
| What you need | `cloudflared` | A bucket, credentials, public read access |
| Good for | Showing a colleague a result today | A paper, a project page, many viewers |

For a link that lasts, see [Publish a permanent link](publish.md). To look at a store yourself, use [`chronozarr preview`](preview.md). For data that must stay private, see [Private stores](private.md).

## Before you start

Install [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) once. It is a free program from Cloudflare that makes the temporary address. It needs no Cloudflare account and no configuration. On macOS:

```bash
brew install cloudflared
```

chronozarr does not install it for you. If it is missing, `share` says so and starts nothing.

## Share a store

1. Start sharing:

   ```bash
   chronozarr share my_store
   ```

   The command prints each stage. The link usually appears after about half a minute. The command waits up to 90 seconds for a new public address to work:

   ```text
   starting local server and quick tunnel...
   waiting for Cloudflare to assign a public address...
   waiting for https://bright-sea-example.trycloudflare.com to work; a new address usually takes 15 to 30 seconds (at most 90)...
   checking browser access...
   measuring first transfer...
   sharing my_store through https://bright-sea-example.trycloudflare.com (byte ranges and CORS verified)
   tunnel throughput: first level-0 chunk 1.55 MB in 0.35 s (4.40 MB/s, 1 data request(s)); estimated overview step 0.35 s for 1 cell(s), assuming similar cell-read sizes
   open: https://chronozarr.org/demo/?store=https%3A%2F%2Fbright-sea-example.trycloudflare.com%2Fmy_store
   The link works while this command runs and this computer stays on and online. Every view is sent from this computer over its upload connection.
   anyone who can open this link can read this store; press Ctrl-C to stop sharing
   ```

   The `tunnel throughput` line times one chunk through the tunnel. It shows how fast a recipient's views will load. Package 0.4.0 prints the address only when it works and takes about a minute and a half ([measurement](evidence.md#time-to-a-link-2026-10-12)).

2. The viewer opens. Choose the date, the product and the part of the map that you want to show.

3. Click Copy link. The copied link holds the store, the date, the product, the band and its display range, the zoom and the map centre. It does not hold the playback speed.

4. Send the copied link. Keep the terminal open and the computer awake while your colleague uses it.

5. Press Ctrl-C in the terminal to stop sharing. The command stops the tunnel and the local server, and the link stops working.

Each run makes a new address. A link from an earlier run does not work again.

## What happens

1. `share` serves the store from your computer on `127.0.0.1`. It serves only the store folder, and the viewer folder when you pass `--viewer-dir`.
2. `cloudflared` gives that server a public https address on `trycloudflare.com`.
3. `share` reads the store through that address, as a browser would, and prints the link only when the read works.

The viewer page comes from chronozarr.org. The data does not: the viewer reads the store through the tunnel, directly from your computer. chronozarr.org holds no copy of your dataset.

## Limits

- Anyone who has the link can read the whole store while `share` runs. Do not share data that must stay private.
- Every view and every timestep that the recipient loads travels over your upload connection. A few viewers are fine. For many viewers, or fast playback of a large area, [publish](publish.md) the store.
- If your computer sleeps, loses its network or stops the command, the link stops working.
- A quick tunnel is a free Cloudflare service for testing, with no uptime guarantee.

## Options

`--port`, `--viewer`, `--viewer-dir` and `--no-open` mean the same as for [`preview`](preview.md#options). Use `--no-open` to print the link without opening a browser.

A tunnel targets a port, not a program. `share` starts its own server and checks that the port serves your store before it starts the tunnel. Do not start a tunnel by hand on a port that you have not checked: if another program owns that port, the tunnel exposes it.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `cloudflared is required ... but was not found` | Install it (see above) and run the command again |
| `cloudflared exited ...` | cloudflared could not reach Cloudflare. Check your network connection, or a firewall that blocks outgoing connections, and run the command again |
| `the tunnel did not serve this store within 90 seconds` | The new address did not start working in time. Run the command again |
| The recipient sees `Shared link not available` | Sharing stopped, or the computer slept or went offline. Start `share` again and send the new link |
| Scrubbing is slow for the recipient | Every read passes through your upload connection. [Publish](publish.md) the store for more than a few viewers |
