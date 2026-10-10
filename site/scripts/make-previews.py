"""Build the illustrative homepage images from the local demo store.

Run from the repository root: uv run python site/scripts/make-previews.py
Requires Pillow in the environment. Does not modify the source store.

Writes to site/docs/public:
  ucayali-plate-2025.webp       August 2025, pyramid level 0 (native 10 m), 5:3 crop, 2759 px wide
  ucayali-plate-2025-1380.webp  the same plate at 1380 px for 1x screens
  ucayali-2016.webp ... ucayali-2025.webp   every August, pyramid level 3, 260 px

All images are false color: near infrared, red, green (B08, B04, B03) with one fixed
display stretch. They are illustrations, not numeric exports.
"""

from pathlib import Path

import numpy as np
from PIL import Image

import chronozarr

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "site/docs/public"
PLATE_DATE = "2025-08"
STRIP_YEARS = range(2016, 2026)  # the store runs 2015-11 to 2026-02; these are the complete Augusts

store = chronozarr.open_store(ROOT / "data/stores/ucayali_santa_maria_v03")
bands = [list(store.bands).index(band) for band in ["B08", "B04", "B03"]]
# Fixed display stretch for every image: NIR clipped at 6000, red and green at 3000, gamma 1.6.
scale = np.array([6000.0, 3000.0, 3000.0], dtype=np.float32)[:, None, None]
times = [str(value)[:7] for value in store.times]


def render(index: int, lod: int) -> Image.Image:
    values = store.read(index, lod=lod)[bands].astype(np.float32)
    stretched = np.clip(values / scale, 0, 1) ** (1 / 1.6)
    return Image.fromarray((stretched * 255).astype(np.uint8).transpose(1, 2, 0))


plate = render(times.index(PLATE_DATE), lod=0)
width, height = plate.size
crop_height = width * 3 // 5
top = (height - crop_height) // 2
plate = plate.crop((0, top, width, top + crop_height))
plate.save(OUT / "ucayali-plate-2025.webp", quality=88, method=6)
plate.resize((1380, 1380 * crop_height // width), Image.LANCZOS).save(
    OUT / "ucayali-plate-2025-1380.webp", quality=86, method=6
)
print("plate", plate.size)

for year in STRIP_YEARS:
    thumb = render(times.index(f"{year}-08"), lod=3)
    thumb.thumbnail((260, 260))
    thumb.save(OUT / f"ucayali-{year}.webp", quality=82)
    print("thumb", year, thumb.size)
