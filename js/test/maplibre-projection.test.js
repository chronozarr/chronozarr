import { registerHooks } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DAYMET_LCC, MODIS_SINUSOIDAL, NSIDC_POLAR_STEREOGRAPHIC_NORTH, WGS84_UTM_31N } from '../support/crs-fixtures.js';
import {
  SUPPORTED_CRS,
  createProjection,
  crsToPixel,
  footprintBounds,
  levelTransform,
  lonLatToMercator,
  mercatorPerTexel,
  mercatorToLonLat,
  pixelToCrs,
} from '../maplibre/projection.js';

// Reference values from pyproj 3.7.2 (PROJ etmerc / EPSG:3857), generated with
//   Transformer.from_crs(4326, epsg, always_xy=True).transform(lon, lat), x/y rounded to 0.1 mm,
// and mx/my = ((x + H) / 2H, (H - y) / 2H) of the EPSG:3857 result, H = 20037508.342789244.
const PYPROJ = [
  { epsg: 32718, lon: -75.0, lat: -7.7, x: 500000.0, y: 9148867.3756, mx: 0.2916666666666667, my: 0.5214535644147362 },
  { epsg: 32718, lon: -75.1234, lat: -7.6543, x: 486390.2007, y: 9153917.5519, mx: 0.2913238888888889, my: 0.5213254718159211 },
  { epsg: 32718, lon: -74.9, lat: -7.8, x: 511025.2281, y: 9137811.0465, mx: 0.2919444444444444, my: 0.5217339028692869 },
  { epsg: 32718, lon: -76.5, lat: -8.5, x: 334890.7123, y: 9060106.1823, mx: 0.2875, my: 0.5236981986594051 },
  { epsg: 32631, lon: 3.0, lat: 0.0, x: 500000.0, y: 0.0, mx: 0.5083333333333334, my: 0.5 },
  { epsg: 32631, lon: 2.0, lat: 48.85, x: 426638.5798, y: 5411263.2972, mx: 0.5055555555555555, my: 0.3440562657322555 },
  { epsg: 32631, lon: 5.9, lat: 45.2, x: 727767.2432, y: 5009260.092, mx: 0.5163888888888889, my: 0.35893798665174786 },
  { epsg: 32631, lon: 0.3, lat: 10.0, x: 203988.4678, y: 1106624.2885, mx: 0.5008333333333334, my: 0.47208011206491635 },
  { epsg: 32633, lon: 15.3, lat: 62.0, x: 515713.1456, y: 6874216.4697, mx: 0.5425, my: 0.2789360171681782 },
  { epsg: 32633, lon: 12.9, lat: -0.5, x: 266278.7364, y: -55302.4271, mx: 0.5358333333333334, my: 0.5013889065175474 },
  { epsg: 32633, lon: 17.0, lat: 80.0, x: 538764.0577, y: 8882252.1697, mx: 0.5472222222222222, my: 0.11225939796299499 },
  { epsg: 32610, lon: -122.4194, lat: 37.7749, x: 551130.7685, y: 4180998.8815, mx: 0.15994611111111118, my: 0.38652093672172827 },
  { epsg: 32610, lon: -123.9, lat: 47.5, x: 432218.1522, y: 5261122.2436, mx: 0.15583333333333335, my: 0.34968010951467365 },
  { epsg: 32755, lon: 151.2093, lat: -33.8688, x: 889449.9971, y: 6244409.9773, mx: 0.9200258333333332, my: 0.6000922514587761 },
  { epsg: 32755, lon: 147.4, lat: -42.9, x: 532656.1834, y: 5250212.3393, mx: 0.9094444444444445, my: 0.6321712036740927 },
  { epsg: 32601, lon: -179.9, lat: 65.0, x: 363283.2171, y: 7211591.2522, mx: 0.000277777777777776, my: 0.26024036158688973 },
  { epsg: 32660, lon: 179.9, lat: -60.0, x: 661720.7509, y: -6654956.7199, mx: 0.9997222222222222, my: 0.7096003591394914 },
  { epsg: 32740, lon: 57.5, lat: -20.2, x: 552236.7476, y: 7766307.7371, mx: 0.6597222222222222, my: 0.5573109875979588 },
  { epsg: 32745, lon: 90.3, lat: 23.8, x: 836289.4642, y: 12635995.082, mx: 0.7508333333333332, my: 0.4319013107581521 },
];

test('UTM forward matches pyproj to 0.2 mm in north and south zones, at zone edges and high latitude', async () => {
  for (const p of PYPROJ) {
    const [x, y] = (await createProjection(`EPSG:${p.epsg}`)).fromLonLat(p.lon, p.lat);
    assert.ok(Math.abs(x - p.x) < 2e-4 && Math.abs(y - p.y) < 2e-4, `EPSG:${p.epsg} (${p.lon}, ${p.lat}): got ${x}, ${y}, expected ${p.x}, ${p.y}`);
  }
});

test('UTM inverse matches pyproj lon/lat to 2e-9 degrees', async () => {
  for (const p of PYPROJ) {
    const [lon, lat] = (await createProjection(`EPSG:${p.epsg}`)).toLonLat(p.x, p.y);
    assert.ok(Math.abs(lon - p.lon) < 2e-9 && Math.abs(lat - p.lat) < 2e-9, `EPSG:${p.epsg}: got ${lon}, ${lat}, expected ${p.lon}, ${p.lat}`);
  }
});

test('UTM round trips to a micrometre across zones and hemispheres, 3 degrees either side of the central meridian', async () => {
  for (const epsg of [32601, 32618, 32631, 32660, 32718, 32755, 32760]) {
    const projection = await createProjection(`EPSG:${epsg}`);
    const south = epsg >= 32700;
    const zone = epsg % 100;
    const lon0 = (zone - 1) * 6 - 177;
    for (const dLon of [-3, -1.5, 0, 0.7, 3]) {
      for (const lat of south ? [-80, -45, -7.7, -0.2] : [0.2, 7.7, 45, 80]) {
        const lon = Math.max(-180, Math.min(180, lon0 + dLon));
        const [x, y] = projection.fromLonLat(lon, lat);
        const [lon2, lat2] = projection.toLonLat(x, y);
        const [x2, y2] = projection.fromLonLat(lon2, lat2);
        assert.ok(Math.abs(x2 - x) < 1e-6 && Math.abs(y2 - y) < 1e-6, `EPSG:${epsg} ${lon},${lat}: round trip moved by ${x2 - x}, ${y2 - y} m`);
        assert.ok(Math.abs(lon2 - lon) < 1e-9 && Math.abs(lat2 - lat) < 1e-9, `EPSG:${epsg} ${lon},${lat}: came back as ${lon2},${lat2}`);
      }
    }
  }
});

test('UTM known points: central meridian at the equator, false northing in the south, scale factor on the axis', async () => {
  assert.deepEqual((await createProjection('EPSG:32631')).fromLonLat(3, 0), [500000, 0]);
  const [x, y] = (await createProjection('EPSG:32718')).fromLonLat(-75, 0);
  assert.equal(x, 500000);
  assert.ok(Math.abs(y - 10000000) < 1e-6);
  // One degree of latitude on the central meridian is 110574.4 m of meridian arc at the equator; UTM scales it by 0.9996.
  const [, y1] = (await createProjection('EPSG:32631')).fromLonLat(3, 1);
  assert.ok(Math.abs(y1 - 110574.3886 * 0.9996) < 0.05, `got ${y1}`);
});

test('web mercator normalised coordinates match pyproj EPSG:3857 and invert exactly', () => {
  for (const p of PYPROJ) {
    const [mx, my] = lonLatToMercator(p.lon, p.lat);
    assert.ok(Math.abs(mx - p.mx) < 1e-12 && Math.abs(my - p.my) < 1e-12, `(${p.lon}, ${p.lat}): got ${mx}, ${my}, expected ${p.mx}, ${p.my}`);
    const [lon, lat] = mercatorToLonLat(mx, my);
    assert.ok(Math.abs(lon - p.lon) < 1e-9 && Math.abs(lat - p.lat) < 1e-9, `(${p.lon}, ${p.lat}) came back as ${lon}, ${lat}`);
  }
  assert.deepEqual(lonLatToMercator(0, 0), [0.5, 0.5]);
});

test('EPSG:3857 and EPSG:4326 stores are supported and consistent with the mercator helpers', async () => {
  const web = await createProjection('EPSG:3857');
  const [wx, wy] = web.fromLonLat(180, 0);
  assert.ok(Math.abs(wx - 20037508.342789244) < 1e-6 && Math.abs(wy) < 1e-6);
  const [lon, lat] = web.toLonLat(...web.fromLonLat(-75.1, -7.6));
  assert.ok(Math.abs(lon + 75.1) < 1e-12 && Math.abs(lat + 7.6) < 1e-12);
  assert.deepEqual((await createProjection('EPSG:4326')).toLonLat(-75, -7), [-75, -7]);
});

test('a code the built-ins do not cover, with no definition to read, fails naming the code and what would fix it; UTM zones outside 1-60 are rejected', async () => {
  for (const crs of ['EPSG:2154', 'EPSG:32661', 'EPSG:32600', 'EPSG:32800', 'epsg:32718', 'WGS84', '']) {
    await assert.rejects(createProjection(crs), (error) => error.message.includes(`"${crs}"`) && error.message.includes(SUPPORTED_CRS) && /proj:wkt2/.test(error.message), crs);
  }
  await assert.rejects(createProjection({}), /declares no CRS/);
});

test('affine transforms: pixel corners to CRS and back, with rotation; singular transforms throw', () => {
  const transform = [10, 0.5, 485650, 0.25, -10, 9169880];
  for (const [col, row] of [[0, 0], [100.25, 37.5], [2759, 2765]]) {
    const [x, y] = pixelToCrs(transform, col, row);
    const [c, r] = crsToPixel(transform, x, y);
    assert.ok(Math.abs(c - col) < 1e-9 && Math.abs(r - row) < 1e-9);
  }
  assert.deepEqual(pixelToCrs([10, 0, 485650, 0, -10, 9169880], 1, 2), [485660, 9169860]);
  assert.throws(() => crsToPixel([1, 2, 0, 2, 4, 0], 1, 1), /singular/);
});

test('level transforms double the pixel size and keep the origin', () => {
  const level0 = [10, 0, 485650, 0, -10, 9169880];
  assert.deepEqual(levelTransform(level0, 0), level0);
  assert.deepEqual(levelTransform(level0, 3), [80, 0, 485650, 0, -80, 9169880]);
  // The corner of level-2 pixel (5, 7) is the corner of level-0 pixel (20, 28).
  assert.deepEqual(pixelToCrs(levelTransform(level0, 2), 5, 7), pixelToCrs(level0, 20, 28));
});

test('mercatorPerTexel matches pyproj: mean east and south step of a 10 m pixel in EPSG:3857 metres', async () => {
  const worldMetres = 40075016.68557849;
  // pyproj, UTM 18S store centre: east 10.0928879 m, south 10.1597092 m; UTM 31N at the equator on the central meridian: 10.0040016 m and 10.0714235 m.
  const ucayali = mercatorPerTexel(await createProjection('EPSG:32718'), [10, 0, 485650, 0, -10, 9169880], 2759, 2765);
  assert.ok(Math.abs(ucayali * worldMetres - 10.126298563) < 1e-5, `got ${ucayali * worldMetres} m`);
  const equator = mercatorPerTexel(await createProjection('EPSG:32631'), [10, 0, 499000, 0, -10, 1000], 200, 200);
  assert.ok(Math.abs(equator * worldMetres - 10.037712569) < 1e-5, `got ${equator * worldMetres} m`);
});

test('footprintBounds of the Ucayali store matches the pyproj outline to 1e-7 degrees', async () => {
  // Extremes of the pyproj outline of the 2759 x 2765 px, 10 m store at (485650, 9169880) in EPSG:32718.
  const [[west, south], [east, north]] = footprintBounds(await createProjection('EPSG:32718'), [10, 0, 485650, 0, -10, 9169880], 2759, 2765);
  assert.ok(Math.abs(west - -75.13014366633182) < 1e-7, `west ${west}`);
  assert.ok(Math.abs(south - -7.7600395081901565) < 1e-7, `south ${south}`);
  assert.ok(Math.abs(east - -74.87992317992743) < 1e-7, `east ${east}`);
  assert.ok(Math.abs(north - -7.509906374016171) < 1e-7, `north ${north}`);
});

// ---- CRSs without a built-in projection: proj4 from the store's WKT2 or PROJJSON ----

const METRES_PER_DEGREE = 111320;
const groundDistance = (lon, lat, lon2, lat2) => Math.hypot((lon2 - lon) * Math.cos((lat * Math.PI) / 180) * METRES_PER_DEGREE, (lat2 - lat) * METRES_PER_DEGREE);

for (const [label, fixture, source] of [
  ['Daymet LCC from WKT2', DAYMET_LCC, { wkt2: DAYMET_LCC.wkt2 }],
  ['Daymet LCC from PROJJSON', DAYMET_LCC, { projjson: DAYMET_LCC.projjson }],
  ['MODIS sinusoidal from WKT2', MODIS_SINUSOIDAL, { wkt2: MODIS_SINUSOIDAL.wkt2 }],
  ['EPSG:3413 from its code and WKT2', NSIDC_POLAR_STEREOGRAPHIC_NORTH, { crs: 'EPSG:3413', wkt2: NSIDC_POLAR_STEREOGRAPHIC_NORTH.wkt2 }],
]) {
  test(`${label}: forward and inverse match pyproj to 1 mm at 7 points`, async () => {
    const projection = await createProjection(source);
    assert.equal(projection.kind, 'proj4');
    assert.equal(fixture.points.length, 7);
    for (const [lon, lat, x, y] of fixture.points) {
      const [fx, fy] = projection.fromLonLat(lon, lat);
      assert.ok(Math.abs(fx - x) < 1e-3 && Math.abs(fy - y) < 1e-3, `(${lon}, ${lat}): got ${fx}, ${fy}, expected ${x}, ${y}`);
      const [lon2, lat2] = projection.toLonLat(x, y);
      assert.ok(groundDistance(lon, lat, lon2, lat2) < 1e-3, `(${x}, ${y}) came back as ${lon2}, ${lat2}, expected ${lon}, ${lat}`);
    }
  });
}

for (const [label, fixture, source] of [['Daymet LCC', DAYMET_LCC, { wkt2: DAYMET_LCC.wkt2 }], ['MODIS sinusoidal', MODIS_SINUSOIDAL, { wkt2: MODIS_SINUSOIDAL.wkt2 }]]) {
  test(`${label}: footprint and pixel centres of a 200 x 200 store match the pyproj outline and inverse to 1e-8 degrees`, async () => {
    const projection = await createProjection(source);
    const { transform, size, outline, centres } = fixture.grid;
    const [[west, south], [east, north]] = footprintBounds(projection, transform, size, size);
    for (const [name, got, expected] of [['west', west, outline[0]], ['south', south, outline[1]], ['east', east, outline[2]], ['north', north, outline[3]]]) {
      assert.ok(Math.abs(got - expected) < 1e-8, `${name} ${got}, expected ${expected}`);
    }
    for (const [col, row, lon, lat] of centres) {
      const [x, y] = pixelToCrs(transform, col + 0.5, row + 0.5);
      const [lon2, lat2] = projection.toLonLat(x, y);
      assert.ok(Math.abs(lon2 - lon) < 1e-8 && Math.abs(lat2 - lat) < 1e-8, `pixel ${col}, ${row}: got ${lon2}, ${lat2}, expected ${lon}, ${lat}`);
    }
  });
}

test('the WKT2 wins when the code names another CRS', async () => {
  const projection = await createProjection({ crs: 'EPSG:32631', wkt2: MODIS_SINUSOIDAL.wkt2 });
  assert.equal(projection.kind, 'proj4');
  const [lon, lat, x, y] = MODIS_SINUSOIDAL.points[1];
  const [fx, fy] = projection.fromLonLat(lon, lat);
  assert.ok(Math.abs(fx - x) < 1e-3 && Math.abs(fy - y) < 1e-3, `got ${fx}, ${fy}`);
});

test('a code the built-ins know stays on them when the WKT2 ends in the same ID, and a store object works as the source', async () => {
  const store = { crs: 'EPSG:32631', wkt2: WGS84_UTM_31N.wkt2, projjson: null };
  const projection = await createProjection(store);
  assert.equal(projection.kind, 'utm');
  assert.deepEqual(projection.fromLonLat(3, 0), [500000, 0]);
  assert.equal((await createProjection({ crs: null, wkt2: WGS84_UTM_31N.wkt2 })).kind, 'utm', 'the ID of the WKT2 is enough without the code');
  assert.equal((await createProjection({ projjson: { type: 'ProjectedCRS', id: { authority: 'EPSG', code: 3857 } } })).kind, 'web-mercator');
});

test('proj4 is imported when no built-in projection matches, and never for an EPSG store', async () => {
  const requested = [];
  const hooks = registerHooks({
    resolve(specifier, context, nextResolve) {
      requested.push(specifier);
      return nextResolve(specifier, context);
    },
  });
  try {
    for (const source of ['EPSG:32631', 'EPSG:32718', 'EPSG:3857', 'EPSG:4326', { crs: 'EPSG:32631', wkt2: WGS84_UTM_31N.wkt2 }]) await createProjection(source);
    assert.deepEqual(requested.filter((specifier) => /proj4|vendor/.test(specifier)), [], 'EPSG stores import nothing');
    await createProjection({ wkt2: DAYMET_LCC.wkt2 });
    assert.deepEqual(requested.filter((specifier) => /proj4/.test(specifier)), ['../vendor/proj4/proj4.js']);
  } finally {
    hooks.deregister();
  }
});

test('a definition proj4 cannot read, or a projection outside the bundled set, fails with the reason and the set', async () => {
  const mollweide = 'PROJCRS["World_Mollweide",BASEGEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]]],CONVERSION["World_Mollweide",METHOD["Mollweide"],PARAMETER["Longitude of natural origin",0],PARAMETER["False easting",0],PARAMETER["False northing",0]],CS[Cartesian,2],AXIS["(E)",east],AXIS["(N)",north],LENGTHUNIT["metre",1]]';
  for (const wkt2 of [mollweide, 'not a CRS']) {
    await assert.rejects(createProjection({ wkt2 }), (error) => error instanceof Error && /cannot place a store in this CRS/.test(error.message) && /lcc/.test(error.message) && error.message.length < 600, wkt2.slice(0, 20));
  }
});
