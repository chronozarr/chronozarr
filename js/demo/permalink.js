// The part of the viewer state that goes in the URL, as pure functions.
//
//   ?t=71&p=ndvi&z=2.5&c=684500,9123350        (plus &b=B04&r=0,0.4 for the single-band product)
//
//   t  timestep index            p  product id             b  band name (single-band product)
//   r  display limits low,high in physical units (single-band product whose limits are adjustable; the
//      viewer ignores it for a band shown as reflectance and for any other product)
//   z  zoom, CSS pixels per level-0 data pixel (so the same ground width shows in a window of any size)
//   c  view center: x,y in the store's projected coordinates when it declares an affine transform
//      (whole units when a pixel is 1 or more units wide), otherwise x,y in level-0 pixels

/** The six affine coefficients [a, b, c, d, e, f]: x = a*col + b*row + c, y = d*col + e*row + f. */
export function pixelToProjected(transform, col, row) {
  const [a, b, c, d, e, f] = transform;
  return { x: a * col + b * row + c, y: d * col + e * row + f };
}

export function projectedToPixel(transform, x, y) {
  const [a, b, c, d, e, f] = transform;
  const det = a * e - b * d;
  const dx = x - c;
  const dy = y - f;
  return { col: (e * dx - b * dy) / det, row: (a * dy - d * dx) / det };
}

const round = (value, decimals) => Number(value.toFixed(decimals));

/**
 * Query string for a view. Leave a field null/undefined to omit it (the viewer omits what matches the
 * default, so an untouched view has a clean URL). `center` is {col, row} in level-0 pixels.
 */
export function encodeView({ t, productId, bandName, range, zoom, center }, { transform = null } = {}) {
  const params = new URLSearchParams();
  if (t != null) params.set('t', String(t));
  if (productId != null) params.set('p', productId);
  if (bandName != null) params.set('b', bandName);
  if (range != null) params.set('r', `${range[0]},${range[1]}`);
  if (zoom != null) params.set('z', String(Number(zoom.toPrecision(3))));
  if (center != null) {
    if (transform) {
      const { x, y } = pixelToProjected(transform, center.col, center.row);
      const decimals = Math.abs(transform[0]) >= 1 ? 0 : 3;
      params.set('c', `${round(x, decimals)},${round(y, decimals)}`);
    } else {
      params.set('c', `${round(center.col, 1)},${round(center.row, 1)}`);
    }
  }
  return params.toString().replaceAll('%2C', ',');
}

/**
 * The view described by a query string, checked against the store: `count` timesteps, the available
 * `productIds`, the `bands`, and the affine `transform` (or null). Anything missing or invalid is left
 * undefined so the viewer keeps its default for it.
 */
export function decodeView(search, { count, productIds, bands, transform = null }) {
  const params = new URLSearchParams(search);
  const view = {};
  const t = params.get('t');
  if (t !== null && /^\d+$/.test(t) && Number(t) < count) view.t = Number(t);
  const p = params.get('p');
  if (p !== null && productIds.includes(p)) view.productId = p;
  const b = params.get('b');
  if (b !== null && bands.includes(b)) view.bandName = b;
  const limits = (params.get('r') ?? '').split(',');
  if (limits.length === 2 && limits.every((part) => part.trim() !== '' && Number.isFinite(Number(part))) && Number(limits[0]) < Number(limits[1])) {
    view.range = limits.map(Number);
  }
  const z = Number(params.get('z'));
  if (params.get('z') !== null && Number.isFinite(z) && z > 0) view.zoom = z;
  const parts = (params.get('c') ?? '').split(',');
  if (parts.length === 2 && parts.every((part) => part.trim() !== '' && Number.isFinite(Number(part)))) {
    const [x, y] = parts.map(Number);
    view.center = transform ? (({ col, row }) => ({ col, row }))(projectedToPixel(transform, x, y)) : { col: x, row: y };
  }
  return view;
}
