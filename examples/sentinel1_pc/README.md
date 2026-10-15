# Sentinel-1 backscatter

`ingest.py` builds a store of monthly Sentinel-1 backscatter composites over one box from Planetary Computer's `sentinel-1-rtc` collection: VV and VH gamma0 with radiometric terrain correction, read as windows of each scene's 10 m grid by HTTP range requests, averaged to 20 m and to months. It needs no login. Run the commands from the repository root.

## The Ucayali store

The store `ucayali_santa_maria/s1-rtc-monthly-v1` covers the same box as the Sentinel-2 store `ucayali_santa_maria_v03`, the Santa María reach of the Ucayali River in Peru, on the same grid coarsened 2x: its 20 m pixels are the pixels of that store's level 1. Radar sees through the wet-season cloud that leaves gaps in the optical months. It holds relative orbit 171 descending from 2017-01 to 2026-09: 309 acquisitions in 110 monthly composites. Seven months have no acquisition on that orbit (2018-07 to 2018-10 and 2022-01 to 2022-03, after Sentinel-1B failed) and are left out of the time axis. Build it with:

```sh
uv sync --extra ingest
uv run python examples/sentinel1_pc/ingest.py --bbox -75.13 -7.76 -74.88 -7.51 \
    --name ucayali_santa_maria --start 2017-01-01 --end 2026-09-30
```

Add `--dry-run` to list the acquisitions and the empty months and write nothing. Each acquisition reads about 70 MB of float32 (VV and VH windows of a 28 km box), so a full run moves about 21 GB from Planetary Computer's West Europe storage; eight acquisitions are read at once (`--workers`). It writes:

| Path | What |
|---|---|
| `data/stores/ucayali_santa_maria/s1-rtc-monthly-v1` | the store. `--store` names it; a re-encode takes a new name, because a published store is immutable |
| `data/stores/ucayali_santa_maria/s1-rtc-monthly-v1.scenes.json` | the manifest: the query, the orbit, the grid, every acquisition (slices, platform, valid fraction, dB percentiles) and every month (its acquisitions, valid fraction, dB percentiles) |
| `data/cache/sentinel1/ucayali_santa_maria/<slice id>.npz` | the 20 m VV and VH power of each acquisition with its record beside it; a rerun reads these instead of the scenes |

Then check it, share it and look at it:

```sh
uv run chronozarr doctor data/stores/ucayali_santa_maria/s1-rtc-monthly-v1
uv run chronozarr share data/stores/ucayali_santa_maria/s1-rtc-monthly-v1     # a temporary public link; needs cloudflared
uv run chronozarr preview data/stores/ucayali_santa_maria/s1-rtc-monthly-v1   # or: this computer only
```

The viewer opens it with the dual-pol composite: red is VV, green is VH and blue is VV minus VH (the co/cross ratio in dB), each stretched from the 2nd to the 98th percentile of the first month on screen and held while the months change. Open water is dark, forest is bright in VH, bare sand bars are high in the ratio. In Python the backend applies the band scale and offset, so the values are dB with NaN where the mask is 0 (`physical=False` gives the stored uint16):

```python
import xarray as xr

ds = xr.open_dataset("data/stores/ucayali_santa_maria/s1-rtc-monthly-v1", engine="chronozarr")
vh = ds["data"].sel(band="VH")                       # dB, lazily
power = 10 ** (vh / 10)                              # gamma0 power, for means and ratios
```

### Your own box

`--bbox W S E N` (degrees) and `--name` are the only required arguments. `ingest.py` asks Planetary Computer for IW scenes with VV and VH over the box between `--start` (default 2017-01-01) and `--end` (default the last day of the previous month, so every month is complete), and takes the relative orbit with the most acquisition dates, so every month sees the box from the same geometry; `--orbit` pins one. The slices of one pass are mosaicked into one acquisition. The grid is the box snapped outwards to whole 10 m pixels in the UTM zone of its centre, as `examples/sentinel2_pc` builds its grid, then coarsened 2x. Scenes are read without a warp: a scene in another UTM zone, or one not offset from the grid by whole pixels, stops the run before anything is read, so keep the box inside one zone.

Some items' `proj:transform` does not describe the file (six 2024 S1A items over the Ucayali box carry a fractional, south-up transform; their files are north-up on whole 10 m). The ingest takes the window from each file's own header and checks the alignment there.

### What is stored

- `VV` and `VH` are gamma0 backscatter power as delivered, as `10 * log10(power)` in dB, quantized to 0.01 dB in uint16: stored = (dB + 60) * 100, with scale 0.01 and offset -60 in the band metadata, the same encoding as the NISAR store. Values below -60 dB are clipped to -60 dB.
- A 10 m sample is valid when VV and VH are finite, above zero and not the product's nodata (-32768). Each 20 m pixel is the mean linear power of its 2 x 2 samples, valid when all four are: a 2 x 2 multilook, close to the IW GRD resolution of about 20 x 22 m, that lowers speckle and holds a quarter of the bytes.
- A month is the mean linear power of its acquisitions that are valid at the pixel, and `mask` is 1 where at least one is. Invalid pixels store 0. Months are stamped on their first day.
- The pyramid averages dB, as the spec's mean pyramid averages stored values, which is the geometric mean of power. Means and ratios in power belong in Python, on level 0.
- Provenance names the collection and the compositing; the manifest names every slice.
