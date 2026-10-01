#!/usr/bin/env bash
# Frames from render_frames.py to a chronozarr store: the PNGs and their world files go straight
# in, no GeoTIFF step. The world file carries the transform but no CRS, so --crs says which one.
set -euo pipefail

cd "$(dirname "$0")/../.."

frames=data/png_frames/ucayali
store=data/stores/ucayali_santa_maria/png-1

uv run chronozarr convert "$frames/manifest.csv" "$store" --crs EPSG:32718
uv run chronozarr validate "$store"
