// What a system draws at the screen position of one level-0 pixel, read back from its WebGL canvas.
//
// In the zoom view a level-0 pixel is 1.45 CSS px = 2.9 device px wide, so every system draws it as a block of about
// 3 x 3 device pixels in one colour (nearest-neighbour sampling). `readPatchInPage` reads a 13 x 13 patch around the
// position the system's own projection gives for the pixel's centre; `blockAround` finds the uniform block that holds the
// centre and says where its middle lies against that centre. A system that draws the pixel in the right place has the block
// middle within half a level-0 pixel (1.45 device px) of the centre in both directions. System C draws tiles that TiTiler
// reprojects and resamples (nearest) onto a Web Mercator grid of about 4 m (zoom-14, 512 px tiles) or 9.5 m pixels, so its
// blocks are snapped to that grid and lie up to about one device px off; the other two draw on the GPU
// from the data directly.

export const PATCH_HALF = 6;

/** Runs in the page: the device pixels around a canvas position. `canvasKind` is 'viewer' (the chronozarr viewer's canvas) or 'map' (MapLibre's). */
export function readPatchInPage({ canvasKind, x, y, half }) {
  const canvas = canvasKind === 'viewer' ? window.chronozarr.viewer.canvas : document.querySelector('canvas.maplibregl-canvas');
  const gl = canvas.getContext('webgl2');
  const rect = canvas.getBoundingClientRect();
  const scale = canvas.width / rect.width;
  const centre = [(x - rect.left) * scale, (y - rect.top) * scale];
  const origin = [Math.floor(centre[0]) - half, Math.floor(centre[1]) - half];
  const size = 2 * half + 1;
  const rgb = [];
  const pixel = new Uint8Array(4);
  for (let i = 0; i < size; i++) {
    for (let j = 0; j < size; j++) {
      gl.readPixels(origin[0] + j, canvas.height - 1 - (origin[1] + i), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
      rgb.push(pixel[0], pixel[1], pixel[2]);
    }
  }
  return { centre, origin, size, rgb };
}

/** The block of one colour around the centre of a patch: its colour and size, and the offset of its middle from the centre (device px). */
export function blockAround({ centre, origin, size, rgb }) {
  const at = (i, j) => rgb.slice((i * size + j) * 3, (i * size + j) * 3 + 3);
  const same = (a, b) => a.every((v, k) => v === b[k]);
  const ci = Math.floor(centre[1]) - origin[1];
  const cj = Math.floor(centre[0]) - origin[0];
  const run = (get, start) => {
    let lo = start;
    let hi = start;
    while (lo > 0 && same(get(lo - 1), get(start))) lo--;
    while (hi < size - 1 && same(get(hi + 1), get(start))) hi++;
    return { lo, hi, truncated: lo === 0 || hi === size - 1 };
  };
  const horizontal = run((j) => at(ci, j), cj);
  const vertical = run((i) => at(i, cj), ci);
  return {
    colour: at(ci, cj),
    widthPx: horizontal.hi - horizontal.lo + 1,
    heightPx: vertical.hi - vertical.lo + 1,
    offsetX: origin[0] + (horizontal.lo + horizontal.hi + 1) / 2 - centre[0],
    offsetY: origin[1] + (vertical.lo + vertical.hi + 1) / 2 - centre[1],
    truncated: horizontal.truncated || vertical.truncated,
  };
}

/** Whether a block sits where its pixel should be: middle within half a level-0 pixel, size about one level-0 pixel, not cut by the patch. */
export function placedRight(block, devicePxPerPixel) {
  const half = devicePxPerPixel / 2;
  return !block.truncated && Math.abs(block.offsetX) <= half && Math.abs(block.offsetY) <= half && block.widthPx >= 1 && block.widthPx <= Math.ceil(devicePxPerPixel) + 1 && block.heightPx >= 1 && block.heightPx <= Math.ceil(devicePxPerPixel) + 1;
}
