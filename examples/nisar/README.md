# NISAR backscatter

`ingest.py` builds a store of NISAR L2 GCOV backscatter over one box straight from the ASF DAAC: it needs only a free [NASA Earthdata login](https://urs.earthdata.nasa.gov) and reads the box out of each granule by HTTP range requests, so nothing of the 2 GB granules lands on disk. Run the commands from the repository root.

## The public store

The public store `https://data.chronozarr.org/colorado_yuma/nisar-gcov-v2` is the Lower Colorado River at Yuma: Imperial and Laguna dams, Mittry Lake, the Gila confluence, the Yuma and Mexicali valleys and the border reach, 39 km by 46 km, from NISAR track 5 ascending, frame 18. It holds every provisional GCOV date of that frame between 2025-12-04 and 2026-10-12, 26 dates 12 days apart, HH and HV at the product's 10 m posting. The frame's two earlier dates, 2025-10-29 (a partial frame) and 2025-11-22 (unfocused samples over the west), cover only half the box and are left out by `--start`. It was built with:

```sh
uv sync --extra nisar
uv run python examples/nisar/ingest.py --bbox -114.82 32.50 -114.42 32.90 --name colorado_yuma \
    --start 2025-12-01 --store nisar-gcov-v2
```

Add `--dry-run` to list the dates and the window and write nothing. The run takes about ten minutes: a minute per granule with six read at once (`--workers`), then the encode. It writes:

| Path | What |
|---|---|
| `data/stores/colorado_yuma/nisar-gcov-v2` | the store: 26 timesteps, 5 levels, 1.9 GB in 5,308 objects. `--store` names it; a re-encode takes a new name, because a published store is immutable |
| `data/stores/colorado_yuma/nisar-gcov-v2.scenes.json` | the manifest: the ASF query, every granule's ASF properties and HDF5 identification, the window, the valid fraction and dB percentiles per date |
| `data/cache/nisar/colorado_yuma/<granule>.npz` | the window of each granule; a rerun reads these instead of the granules |

Then check it and look at it:

```sh
uv run chronozarr doctor data/stores/colorado_yuma/nisar-gcov-v2
uv run chronozarr preview data/stores/colorado_yuma/nisar-gcov-v2
```

In the viewer the single-band product shows HH or HV in dB with a percentile stretch; the chart follows one pixel over the 26 dates. In Python the backend applies the band scale and offset, so the values are dB with NaN where the mask is 0 (`physical=False` gives the stored uint16):

```python
import xarray as xr

ds = xr.open_dataset("data/stores/colorado_yuma/nisar-gcov-v2", engine="chronozarr")
hv = ds["data"].sel(band="HV")                       # dB, lazily
power = 10 ** (hv / 10)                              # gamma0 power, for means and ratios
```

### Your own box

`--bbox W S E N` (degrees) and `--name` are the only required arguments. `ingest.py` asks ASF for standard-production GCOV scenes over the box in one collection maturity (`--collection PROVISIONAL`, the default, or `BETA`), keeps the dual-pol HH+HV scenes, and takes the track, orbit direction and frame with the most dates, so every timestep sees the box from the same geometry; `--track`, `--direction A|D` and `--frame` pin one, `--start` and `--end` bound the dates. A date acquired twice keeps the higher product counter. The box is cut on the first granule's UTM grid, pixel for pixel; a granule on another grid stops the run. A box that crosses two frames gets the better-covered frame only, so keep it inside one frame (about 240 km along track).

### What is stored

- `HH` and `HV` are the GCOV `HHHH` and `HVHV` terms, gamma0 backscatter power with the product's radiometric terrain correction, as `10 * log10(power)` in dB, quantized to 0.01 dB in uint16: stored = (dB + 60) * 100, with scale 0.01 and offset -60 in the band metadata. Values below -60 dB are clipped to -60 dB. 0.01 dB is far below the product's radiometric accuracy; the quantization is the only change to the values.
- `mask` is 1 where the GCOV mask marks a fully focused sample of a subswath (neither 0 nor 255), both powers are finite and both are above zero. Invalid pixels store 0. Every date of the Yuma store is at least 99.99 % valid; a partial frame or unfocused samples would show up as mask 0, and the manifest gives the valid fraction per date.
- The pyramid averages dB, as the spec's mean pyramid averages stored values, which is the geometric mean of power and sits below the linear mean. Level 0 holds the quantized product values; means and ratios in power belong in Python, on level 0.
- Provenance names every granule; the manifest holds the rest. NISAR collection maturities are not mixed: a store is PROVISIONAL or BETA.

The granules are read, not downloaded, so the manifest records the MD5 that ASF reports for each granule, not one computed here.
