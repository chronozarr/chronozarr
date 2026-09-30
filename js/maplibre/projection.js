// Map projections for the MapLibre adapter, in plain JS (no dependencies).
//
// A chronozarr store lives in one projected CRS (normally a UTM zone). MapLibre draws in spherical Web
// Mercator, normalised so the world is the unit square (x east, y south), the same convention as
// maplibregl.MercatorCoordinate. `createProjection(crs)` gives the store CRS <-> lon/lat step;
// `lonLatToMercator` / `mercatorToLonLat` give the lon/lat <-> MapLibre step. All in float64.
//
// UTM uses Karney's (2011) Kruger n-series to 6th order, the same series PROJ's `etmerc` evaluates:
// accurate to nanometres within several thousand km of the central meridian.

const DEG = Math.PI / 180;
const WGS84_A = 6378137;
const WGS84_F = 1 / 298.257223563;
const UTM_K0 = 0.9996;
const UTM_FALSE_EASTING = 500000;
const UTM_FALSE_NORTHING_SOUTH = 10000000;

const n = WGS84_F / (2 - WGS84_F);
const e = Math.sqrt(WGS84_F * (2 - WGS84_F));
const n2 = n * n;
const n3 = n2 * n;
const n4 = n3 * n;
const n5 = n4 * n;
const n6 = n5 * n;
const RECTIFYING_RADIUS = (WGS84_A / (1 + n)) * (1 + n2 / 4 + n4 / 64 + n6 / 256);

// Karney 2011, eqs. 35 (alpha: forward) and 36 (beta: inverse), index 0 is j = 1.
const ALPHA = [
  n / 2 - (2 / 3) * n2 + (5 / 16) * n3 + (41 / 180) * n4 - (127 / 288) * n5 + (7891 / 37800) * n6,
  (13 / 48) * n2 - (3 / 5) * n3 + (557 / 1440) * n4 + (281 / 630) * n5 - (1983433 / 1935360) * n6,
  (61 / 240) * n3 - (103 / 140) * n4 + (15061 / 26880) * n5 + (167603 / 181440) * n6,
  (49561 / 161280) * n4 - (179 / 168) * n5 + (6601661 / 7257600) * n6,
  (34729 / 80640) * n5 - (3418889 / 1995840) * n6,
  (212378941 / 319334400) * n6,
];
const BETA = [
  n / 2 - (2 / 3) * n2 + (37 / 96) * n3 - (1 / 360) * n4 - (81 / 512) * n5 + (96199 / 604800) * n6,
  (1 / 48) * n2 + (1 / 15) * n3 - (437 / 1440) * n4 + (46 / 105) * n5 - (1118711 / 3870720) * n6,
  (17 / 480) * n3 - (37 / 840) * n4 - (209 / 4480) * n5 + (5569 / 90720) * n6,
  (4397 / 161280) * n4 - (11 / 504) * n5 - (830251 / 7257600) * n6,
  (4583 / 161280) * n5 - (108847 / 3991680) * n6,
  (20648693 / 638668800) * n6,
];

/** Conformal latitude parameter tau' = tan(chi) from tau = tan(phi) (Karney eq. 7). */
function tauPrime(tau) {
  const sigma = Math.sinh(e * Math.atanh((e * tau) / Math.hypot(1, tau)));
  return tau * Math.hypot(1, sigma) - sigma * Math.hypot(1, tau);
}

/** Transverse Mercator (forward) on the WGS84 ellipsoid: lon/lat in degrees to metres from the central meridian, scaled by k0. */
function transverseMercatorForward(lonDeg, latDeg, lon0Deg) {
  let dLon = lonDeg - lon0Deg;
  dLon = dLon - 360 * Math.round(dLon / 360);
  const lam = dLon * DEG;
  const tauP = tauPrime(Math.tan(latDeg * DEG));
  const xiP = Math.atan2(tauP, Math.cos(lam));
  const etaP = Math.asinh(Math.sin(lam) / Math.hypot(tauP, Math.cos(lam)));
  let xi = xiP;
  let eta = etaP;
  for (let j = 1; j <= 6; j++) {
    xi += ALPHA[j - 1] * Math.sin(2 * j * xiP) * Math.cosh(2 * j * etaP);
    eta += ALPHA[j - 1] * Math.cos(2 * j * xiP) * Math.sinh(2 * j * etaP);
  }
  return [UTM_K0 * RECTIFYING_RADIUS * eta, UTM_K0 * RECTIFYING_RADIUS * xi];
}

/** Inverse of transverseMercatorForward: metres from the central meridian (easting) and from the equator (northing) to lon/lat degrees. */
function transverseMercatorInverse(easting, northing, lon0Deg) {
  const xi = northing / (UTM_K0 * RECTIFYING_RADIUS);
  const eta = easting / (UTM_K0 * RECTIFYING_RADIUS);
  let xiP = xi;
  let etaP = eta;
  for (let j = 1; j <= 6; j++) {
    xiP -= BETA[j - 1] * Math.sin(2 * j * xi) * Math.cosh(2 * j * eta);
    etaP -= BETA[j - 1] * Math.cos(2 * j * xi) * Math.sinh(2 * j * eta);
  }
  const lam = Math.atan2(Math.sinh(etaP), Math.cos(xiP));
  const tauP = Math.sin(xiP) / Math.hypot(Math.sinh(etaP), Math.cos(xiP));
  // Newton iteration for tau = tan(phi) given tau' (Karney eqs. 19-21); converges in 2-3 steps.
  const oneMinusE2 = 1 - e * e;
  let tau = tauP;
  for (let i = 0; i < 8; i++) {
    const tauPi = tauPrime(tau);
    const dTau = ((tauP - tauPi) / Math.hypot(1, tauPi)) * ((1 + oneMinusE2 * tau * tau) / (oneMinusE2 * Math.hypot(1, tau)));
    tau += dTau;
    if (Math.abs(dTau) < 1e-14 * Math.max(1, Math.abs(tau))) break;
  }
  return [lon0Deg + lam / DEG, Math.atan(tau) / DEG];
}

function utmProjection(crs, zone, south) {
  const lon0 = (zone - 1) * 6 - 180 + 3;
  const falseNorthing = south ? UTM_FALSE_NORTHING_SOUTH : 0;
  return {
    crs,
    kind: 'utm',
    toLonLat(x, y) {
      return transverseMercatorInverse(x - UTM_FALSE_EASTING, y - falseNorthing, lon0);
    },
    fromLonLat(lon, lat) {
      const [easting, northing] = transverseMercatorForward(lon, lat, lon0);
      return [UTM_FALSE_EASTING + easting, falseNorthing + northing];
    },
  };
}

const WEB_MERCATOR_EXTENT = Math.PI * WGS84_A;
const webMercatorProjection = {
  crs: 'EPSG:3857',
  kind: 'web-mercator',
  toLonLat: (x, y) => [(x / WEB_MERCATOR_EXTENT) * 180, (2 * Math.atan(Math.exp(y / WGS84_A)) - Math.PI / 2) / DEG],
  fromLonLat: (lon, lat) => [lon * DEG * WGS84_A, WGS84_A * Math.log(Math.tan(Math.PI / 4 + (lat * DEG) / 2))],
};

const geographicProjection = { crs: 'EPSG:4326', kind: 'geographic', toLonLat: (x, y) => [x, y], fromLonLat: (lon, lat) => [lon, lat] };

export const SUPPORTED_CRS = 'EPSG:326zz and EPSG:327zz (WGS84 UTM zones 1-60 north and south), EPSG:3857, EPSG:4326';

/**
 * The store CRS <-> lon/lat (degrees, WGS84). Supports WGS84 UTM (EPSG:326zz north, 327zz south), EPSG:3857
 * and EPSG:4326; anything else throws, naming the supported set.
 *
 * @returns {{crs:string, kind:string, toLonLat:(x:number,y:number)=>[number,number], fromLonLat:(lon:number,lat:number)=>[number,number]}}
 */
export function createProjection(crs) {
  const utm = /^EPSG:32([67])(\d{2})$/.exec(crs);
  if (utm) {
    const zone = Number(utm[2]);
    if (zone >= 1 && zone <= 60) return utmProjection(crs, zone, utm[1] === '7');
  }
  if (crs === 'EPSG:3857') return webMercatorProjection;
  if (crs === 'EPSG:4326') return geographicProjection;
  throw new Error(`chronozarr maplibre: unsupported store CRS "${crs}". Supported: ${SUPPORTED_CRS}. Re-encode the store in one of these, or add the projection to js/maplibre/projection.js.`);
}

/** lon/lat in degrees to MapLibre mercator coordinates in [0, 1] (x east from -180, y south from the north limit). */
export function lonLatToMercator(lon, lat) {
  return [(lon + 180) / 360, 0.5 - Math.asinh(Math.tan(lat * DEG)) / (2 * Math.PI)];
}

export function mercatorToLonLat(mx, my) {
  return [mx * 360 - 180, Math.atan(Math.sinh(Math.PI * (1 - 2 * my))) / DEG];
}

/** Pixel corner coordinates (col, row; fractional allowed) to CRS x, y with a rasterio-order affine [a, b, c, d, e, f]. */
export function pixelToCrs(transform, col, row) {
  const [a, b, c, d, ee, f] = transform;
  return [a * col + b * row + c, d * col + ee * row + f];
}

/** Inverse of pixelToCrs. Throws on a singular transform. */
export function crsToPixel(transform, x, y) {
  const [a, b, c, d, ee, f] = transform;
  const det = a * ee - b * d;
  if (!Number.isFinite(det) || det === 0) throw new Error(`chronozarr maplibre: affine transform [${transform}] is singular`);
  const dx = x - c;
  const dy = y - f;
  return [(ee * dx - b * dy) / det, (a * dy - d * dx) / det];
}

/** The transform of pyramid level `lod` from the level-0 one: pixels are 2^lod larger, the origin stays put (b and d scale too; both are 0 for north-up stores). */
export function levelTransform(transform0, lod) {
  const s = 2 ** lod;
  const [a, b, c, d, ee, f] = transform0;
  return [a * s, b * s, c, d * s, ee * s, f];
}

/**
 * Size of one level-0 texel in mercator units ([0, 1] world) at the centre of the store, from `transform`, the
 * level-0 affine. Multiply by 512 * 2^zoom for its size in CSS pixels. Includes the UTM scale factor and the
 * mercator stretch at the store's latitude. The east and south steps are averaged: spherical mercator of geodetic
 * latitude is not conformal against the ellipsoid, so they differ by about 0.7%.
 */
export function mercatorPerTexel(projection, transform, width, height) {
  const at = (col, row) => lonLatToMercator(...projection.toLonLat(...pixelToCrs(transform, col, row)));
  const centre = at(width / 2, height / 2);
  const east = at(width / 2 + 1, height / 2);
  const south = at(width / 2, height / 2 + 1);
  return (Math.hypot(east[0] - centre[0], east[1] - centre[1]) + Math.hypot(south[0] - centre[0], south[1] - centre[1])) / 2;
}

/** Footprint of a width x height raster with affine `transform` as [[west, south], [east, north]] degrees, from 33 points along each edge. */
export function footprintBounds(projection, transform, width, height) {
  const samples = 32;
  let west = Infinity;
  let south = Infinity;
  let east = -Infinity;
  let north = -Infinity;
  for (let i = 0; i <= samples; i++) {
    const f = i / samples;
    for (const [col, row] of [[f * width, 0], [f * width, height], [0, f * height], [width, f * height]]) {
      const [lng, lat] = projection.toLonLat(...pixelToCrs(transform, col, row));
      west = Math.min(west, lng);
      east = Math.max(east, lng);
      south = Math.min(south, lat);
      north = Math.max(north, lat);
    }
  }
  return [[west, south], [east, north]];
}
