# SAR creation/storage benchmark

Run UTC: 2026-10-19T00:36:58Z. Git: `ca799a1b985fb6b4090131632f7574508440d84e`.

Reproduce: `uv run python bench/sar/storage_benchmark.py` (3 repeats, 60 isolated subprocess encodes). No source stores changed; temporary output stores deleted automatically.

Local re-encoding only, 3 dates x 1024x1024 pixels at 3 distributed locations x 2 bands per sensor, 3 pyramid levels; ingestion/network excluded. Source reads and imports excluded from encode timing; process peak RSS includes them. OS caches warm, no fsync. Quantization errors are relative to existing 0.01 dB stored values, not raw sensor powers. No visual or scientific acceptance of lossy cases.

The sample uses the first, middle, and last available dates, at windows near the upper-left, centre, and lower-right respectively. Each window is 1024 × 1024 pixels, two bands. Dates and locations vary together; this is a compression sample, not a controlled time-change comparison. Metadata places those windows on one synthetic grid for the encode experiment. Exact positions, dates, source root metadata hashes, decoded sample hashes, runtime versions, and raw repeats are in storage_results.json.

## Stored baselines

- nisar: 1,885,027,649 bytes / 5,308 files (existing complete store, unchanged).
- sentinel1: 801,425,701 bytes / 3,114 files (existing complete store, unchanged).

## Results

Medians of three independent processes. Savings are total candidate store bytes relative to the matched baseline, including masks, coverage if present, metadata, and all three pyramid levels. Local readback is an entire sampled level 0, not browser chunk-decode time.

| Sensor | Candidate | Encode s | Bytes saved % | Process peak MiB | Local L0 read ms | Added max error dB |
|---|---|---:|---:|---:|---:|---:|
| nisar | baseline | 0.231 | 0.000 | 312.3 | 6.3 | 0.00 |
| nisar | zstd1 | 0.264 | -0.358 | 295.5 | 9.3 | 0.00 |
| nisar | zstd3 | 0.282 | -0.321 | 307.1 | 8.3 | 0.00 |
| nisar | zstd9 | 0.258 | 0.018 | 374.1 | 7.0 | 0.00 |
| nisar | shuffle1 | 0.238 | -0.370 | 295.2 | 6.1 | 0.00 |
| nisar | shuffle5 | 0.235 | -0.011 | 320.9 | 7.0 | 0.00 |
| nisar | quant005 | 0.211 | 9.484 | 344.3 | 7.6 | 0.02 |
| nisar | quant010 | 0.204 | 19.770 | 342.6 | 7.3 | 0.05 |
| nisar | worker1 | 0.305 | 0.000 | 297.6 | 6.1 | 0.00 |
| nisar | chunk256 | 0.250 | 0.074 | 289.7 | 12.0 | 0.00 |
| sentinel1 | baseline | 0.208 | 0.000 | 318.1 | 6.5 | 0.00 |
| sentinel1 | zstd1 | 0.274 | -1.249 | 294.9 | 6.3 | 0.00 |
| sentinel1 | zstd3 | 0.208 | -1.179 | 295.8 | 6.4 | 0.00 |
| sentinel1 | zstd9 | 0.229 | 0.030 | 404.5 | 6.7 | 0.00 |
| sentinel1 | shuffle1 | 0.233 | -1.326 | 295.0 | 6.3 | 0.00 |
| sentinel1 | shuffle5 | 0.233 | -0.238 | 327.8 | 6.8 | 0.00 |
| sentinel1 | quant005 | 0.224 | 14.113 | 345.8 | 7.2 | 0.02 |
| sentinel1 | quant010 | 0.203 | 23.962 | 344.2 | 7.0 | 0.05 |
| sentinel1 | worker1 | 0.249 | 0.000 | 297.8 | 6.4 | 0.00 |
| sentinel1 | chunk256 | 0.266 | -0.942 | 290.8 | 13.4 | 0.00 |

## Interpretation

- Keep zstd level 5 / 512-pixel chunks. No tested lossless codec produces material savings; zstd9 increases memory substantially. Small subsecond encode timings vary across repeats, so minor timing differences are not robust performance gains.
- 256-pixel chunks roughly double local full-window read overhead and do not consistently reduce bytes. Smaller chunks may still help a narrow browser viewport; this experiment does not establish that.
- Optional 0.05 dB or 0.1 dB products could reduce storage by approximately 9–14% or 20–24%, respectively. Those are lossy derivatives requiring explicit scientific and visual acceptance. No rounding change was adopted. Their error is additional error relative to the existing already-quantized 0.01 dB values.
- Quantization keeps scale 0.01 and rounds stored uint16 values to multiples of 5 or 10, preserving invalid pixels. This isolates rounding with the current encoder; a differently scaled representation is not benchmarked.
- All recorded lossless data/mask/coverage pyramid hashes match the baseline (30 comparisons across later repeats). Every case also passed exact level-0 roundtrip against its input. Earlier repeat records lack pyramid hashes because hashing was added during the first matrix run; later repeats cover every case.
- Process peak RSS includes Python imports, reading source windows, retained reference arrays, and encoding. It is not an incremental encoder-memory measure. Quantized cases additionally hold temporary rounding/error arrays. Encoding wall time excludes source loading, quantization, validation hashes, and readback.
- Timings used a local warm OS cache and did not force fsync. Filesystem metadata overhead and object counts are included in stored bytes, but upload/CDN behavior is not measured here. Full-store extrapolation is not warranted from these windows.

Raw measurements and provenance: [storage_results.json](storage_results.json).
