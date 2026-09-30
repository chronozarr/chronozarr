// View-dependent decisions that need no GL: which pyramid level to use at a zoom, and which cells are on screen.

/** MapLibre's world is 512 CSS pixels wide at zoom 0 and doubles per zoom level. */
export const WORLD_PX_AT_ZOOM_0 = 512;

/**
 * Pyramid level for a map zoom: the level whose texels are closest, in log2, to one CSS pixel.
 * `mercatorPerTexel0` is the size of one level-0 texel in mercator units, so one level-0 texel covers
 * `mercatorPerTexel0 * 512 * 2^zoom` CSS pixels. `bias` shifts the choice in levels: 0.5 rounds to the nearest level,
 * 0 picks the largest level whose texels are no bigger than a pixel (the spec's reader rule), negative values
 * pick finer levels (crisper on high-density screens, more data).
 */
export function selectLod({ mercatorPerTexel0, levelCount, zoom, bias = 0.5 }) {
  const pixelsPerTexel0 = mercatorPerTexel0 * WORLD_PX_AT_ZOOM_0 * 2 ** zoom;
  const lod = Math.floor(Math.log2(1 / pixelsPerTexel0) + bias + 1e-9);
  return Math.min(Math.max(lod, 0), levelCount - 1);
}

/**
 * Whether a convex mercator-space polygon (flat [x0, y0, x1, y1, ...]) can touch the screen: its clip-space
 * bounding box overlaps [-1 - margin, 1 + margin]^2. A polygon with any vertex at or behind the camera plane is
 * kept (conservative). `matrix` maps mercator [0, 1] units to clip space, column-major.
 */
export function overlapsView(matrix, polygon, margin = 0) {
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < polygon.length; i += 2) {
    const x = polygon[i];
    const y = polygon[i + 1];
    const w = matrix[3] * x + matrix[7] * y + matrix[15];
    if (w <= 1e-9) return true;
    const cx = (matrix[0] * x + matrix[4] * y + matrix[12]) / w;
    const cy = (matrix[1] * x + matrix[5] * y + matrix[13]) / w;
    minX = Math.min(minX, cx);
    maxX = Math.max(maxX, cx);
    minY = Math.min(minY, cy);
    maxY = Math.max(maxY, cy);
  }
  const limit = 1 + margin;
  return !(maxX < -limit || minX > limit || maxY < -limit || minY > limit);
}

/** Squared clip-space distance from the screen centre to a polygon's centroid; orders cells centre-first. */
export function centreDistance2(matrix, polygon) {
  let x = 0;
  let y = 0;
  for (let i = 0; i < polygon.length; i += 2) {
    x += polygon[i];
    y += polygon[i + 1];
  }
  x /= polygon.length / 2;
  y /= polygon.length / 2;
  const w = matrix[3] * x + matrix[7] * y + matrix[15];
  if (w <= 1e-9) return Infinity;
  const cx = (matrix[0] * x + matrix[4] * y + matrix[12]) / w;
  const cy = (matrix[1] * x + matrix[5] * y + matrix[13]) / w;
  return cx * cx + cy * cy;
}

/**
 * The part of a coarser level's cell that covers a finer cell, for painting the coarse data while the fine
 * cell loads. `fine` = {lod, row, col, extent: {width, height}} (the part of the fine cell to cover, in its texels); `chunk` = {width, height}
 * (the same at every level); `coarseExtentOf(row, col)` gives the same kind of extent for a coarse cell.
 * Returns the coarse cell's row/col and a rectangle in that cell's texels, clamped to its valid extent.
 */
export function coarseFootprint(fine, coarseLod, chunk, coarseExtentOf) {
  const scale = 2 ** (coarseLod - fine.lod);
  const row = Math.floor(fine.row / scale);
  const col = Math.floor(fine.col / scale);
  const coarseExtent = coarseExtentOf(row, col);
  const x0 = (fine.col * chunk.width) / scale - col * chunk.width;
  const y0 = (fine.row * chunk.height) / scale - row * chunk.height;
  return {
    row,
    col,
    rect: {
      x0,
      y0,
      x1: Math.min(x0 + fine.extent.width / scale, coarseExtent.width),
      y1: Math.min(y0 + fine.extent.height / scale, coarseExtent.height),
    },
  };
}
