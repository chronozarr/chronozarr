"""Ground truth for checking where the MapLibre layer draws a store: pyproj positions of its
footprint outline and of texel boundaries around the centre of every pyramid level, as JSON on
stdout.

    uv run --with pyproj python3 js/maplibre/verify/truth.py --epsg 32718 \
        --x0 485650 --y0 9169880 --res 10 --width 2759 --height 2765 > truth.json

x0, y0 are the level-0 origin (the affine's c and f), res the level-0 pixel size; width and
height are level 0's shape (read them from `layer.store.levels[0]`: the array shape is
[time, band, height, width]).
"""

import argparse
import json
import sys

from pyproj import Transformer

parser = argparse.ArgumentParser()
parser.add_argument("--epsg", type=int, required=True)
parser.add_argument("--x0", type=float, required=True)
parser.add_argument("--y0", type=float, required=True)
parser.add_argument("--res", type=float, required=True)
parser.add_argument("--width", type=int, required=True)
parser.add_argument("--height", type=int, required=True)
parser.add_argument("--levels", type=int, default=4)
args = parser.parse_args()

to_lonlat = Transformer.from_crs(args.epsg, 4326, always_xy=True)


def lonlat(x, y):
    return list(to_lonlat.transform(x, y))


# Footprint outline, clockwise from the north-west corner, densified so the curve of the UTM grid
# in lon/lat is followed.
n = 40
x1 = args.x0 + args.width * args.res
y1 = args.y0 - args.height * args.res
xs = [args.x0 + (x1 - args.x0) * i / n for i in range(n + 1)]
ys = [args.y0 + (y1 - args.y0) * i / n for i in range(n + 1)]
outline = (
    [lonlat(x, args.y0) for x in xs[:-1]]
    + [lonlat(x1, y) for y in ys[:-1]]
    + [lonlat(x, y1) for x in reversed(xs[1:])]
    + [lonlat(args.x0, y) for y in reversed(ys[1:])]
)
corners = {
    "nw": lonlat(args.x0, args.y0),
    "ne": lonlat(x1, args.y0),
    "sw": lonlat(args.x0, y1),
    "se": lonlat(x1, y1),
    "centre": lonlat((args.x0 + x1) / 2, (args.y0 + y1) / 2),
}

# Texel boundaries of level k: 51 column lines and 37 row lines around the centre texel, each
# given by two points 50 texels apart.
levels = {}
for lod in range(args.levels):
    res = args.res * 2**lod
    w, h = -(-args.width // 2**lod), -(-args.height // 2**lod)
    kc, rc = w // 2, h // 2
    levels[str(lod)] = {
        "centre": lonlat(args.x0 + kc * res, args.y0 - rc * res),
        "cols": [
            {
                "k": k,
                "a": lonlat(args.x0 + k * res, args.y0 - (rc - 25) * res),
                "b": lonlat(args.x0 + k * res, args.y0 - (rc + 25) * res),
            }
            for k in range(kc - 25, kc + 26)
        ],
        "rows": [
            {
                "m": m,
                "a": lonlat(args.x0 + (kc - 25) * res, args.y0 - m * res),
                "b": lonlat(args.x0 + (kc + 25) * res, args.y0 - m * res),
            }
            for m in range(rc - 18, rc + 19)
        ],
    }
json.dump({"outline": outline, "corners": corners, "levels": levels}, sys.stdout)
