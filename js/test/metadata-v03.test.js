import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { CONVENTIONS, sameCrs } from '../chronozarr/metadata.js';
import { DAYMET_LCC, MODIS_SINUSOIDAL, NSIDC_POLAR_STEREOGRAPHIC_NORTH, WGS84_GEOGRAPHIC, WGS84_PSEUDO_MERCATOR, WGS84_UTM_31N } from '../support/crs-fixtures.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

const spec = { nTime: 3, nBand: 2, height: 40, width: 33, chunk: 32, nLevels: 2, sharded: false, mask: true, coverage: true, levelsMirror: true };
const text = new TextEncoder();
function edit(readable, path, change) {
  const meta = JSON.parse(new TextDecoder().decode(readable.files.get(path)));
  change(meta);
  readable.files.set(path, text.encode(JSON.stringify(meta)));
}
const open = (readable) => openStore('memory://metadata', { store: readable, workers: 0 });

test('literal convention registrations and canonical geometry open without coordinate or group reads', async () => {
  const readable = buildSyntheticStore(spec);
  const store = await open(readable);
  assert.equal(store.crs, 'EPSG:32631');
  assert.deepEqual(store.transform, [10, 0, 0, 0, -10, 0]);
  assert.deepEqual(store.levels[1].transform, [20, 0, 0, 0, -20, 0]);
  assert.equal(store.hasMask, true);
  assert.equal(store.hasCoverage, true);
  assert.ok(readable.log.every(({key}) => key === '/zarr.json' || /\/(data|mask|coverage)\/zarr.json$/.test(key)));
  assert.equal(store.attrs.volatility_path, undefined);
  for (const [name, registration] of Object.entries(CONVENTIONS)) {
    assert.equal(registration.schema_url, `https://raw.githubusercontent.com/zarr-conventions/${name}/refs/tags/v0.1/schema.json`);
    assert.equal(registration.spec_url, `https://github.com/zarr-conventions/${name}/blob/v0.1/README.md`);
  }
});

for (const [label, path, mutate, reason] of [
  ['legacy discovery', '/zarr.json', m => m.attributes.multiscales = [{datasets:[{path:'0'}]}], /multiscales.layout/],
  ['level gap', '/zarr.json', m => m.attributes.multiscales.layout[1].asset = '2', /consecutive level groups/],
  ['wrong relative scale', '/zarr.json', m => m.attributes.multiscales.layout[1].transform.scale = [3,3], /scale \[2,2\]/],
  ['rotated grid', '/0/data/zarr.json', m => m.attributes['spatial:transform'][1] = 1, /north-up/],
  ['CRS disagreement', '/0/data/zarr.json', m => m.attributes['proj:code'] = 'EPSG:32718', /proj:code disagrees/],
  ['CRS mirror that is not a code', '/zarr.json', m => m.attributes.chronozarr.crs = 'utm31', /chronozarr.crs must be a CRS code/],
  ['no CRS declaration', '/0/data/zarr.json', m => delete m.attributes['proj:code'], /level 0 declares no CRS/],
  ['proj:code that is not a code', '/0/data/zarr.json', m => { m.attributes['proj:code'] = 'utm31'; m.attributes['proj:wkt2'] = DAYMET_LCC.wkt2; }, /proj:code must be/],
  ['proj:wkt2 that is not text', '/0/data/zarr.json', m => m.attributes['proj:wkt2'] = 42, /proj:wkt2 must be a non-empty string/],
  ['proj:projjson that is not an object', '/0/data/zarr.json', m => m.attributes['proj:projjson'] = 'ProjectedCRS', /proj:projjson must be an object/],
  ['point registration', '/0/data/zarr.json', m => m.attributes['spatial:registration'] = 'point', /pixel-registered/],
  ['mirror geometry disagreement', '/zarr.json', m => m.attributes.chronozarr.levels[0].transform[2] = 42, /mirror transform/],
  ['band-name disagreement', '/zarr.json', m => m.attributes.chronozarr.band_names.reverse(), /band_names disagrees/],
  ['plane geometry disagreement', '/0/mask/zarr.json', m => m.attributes['spatial:transform'][2] = 1, /auxiliary geometry/],
  ['missing dimension names', '/0/data/zarr.json', m => delete m.dimension_names, /dimension names/],
  ['unknown key encoding', '/0/data/zarr.json', m => m.chunk_key_encoding.name = 'future-keys', /chunk_key_encoding/],
  ['unknown storage extension', '/0/data/zarr.json', m => m.storage_transformers = [{name:'encoded_measurements'}], /unsupported storage_transformers/],
  ['unknown mandatory field', '/zarr.json', m => m.future_encoding = {must_understand:true}, /unsupported required Zarr extension/],
]) {
  test(`v0.3 refuses ${label}`, async () => {
    const readable = buildSyntheticStore(spec);
    edit(readable, path, mutate);
    await assert.rejects(open(readable), reason);
    assert.ok(readable.log.every(({key}) => key.endsWith('/zarr.json')), 'no measurements returned or fetched');
  });
}

test('ignorable Zarr extensions leave ordinary values unchanged', async () => {
  const readable = buildSyntheticStore(spec);
  edit(readable, '/zarr.json', m => m.future_display = {must_understand:false});
  const store = await open(readable);
  assert.equal((await store.getCell(0, 0, 0, 1)).data[0], 1040);
});

for (const [label, path, mutate, note] of [
  ['missing multiscales registration', '/zarr.json', m => m.attributes.zarr_conventions.pop(), /multiscales registration/],
  ['alternate registration URL', '/0/data/zarr.json', m => m.attributes.zarr_conventions[0].spec_url = 'https://github.com/zarr-conventions/proj/blob/deadbeef/README.md', /proj registration/],
  ['absent _ARRAY_DIMENSIONS', '/0/data/zarr.json', m => delete m.attributes._ARRAY_DIMENSIONS, /_ARRAY_DIMENSIONS/],
  ['nearest overview method', '/zarr.json', m => m.attributes.multiscales.resampling_method = 'nearest', /overview levels.*level 0/],
  ['absent overview method', '/zarr.json', m => delete m.attributes.multiscales.resampling_method, /resampling_method/],
  ['grid mirror disagreement', '/zarr.json', m => m.attributes.chronozarr.levels[0].grid = [99,99], /grid mirror.*chunks/],
  ['patch version', '/zarr.json', m => m.attributes.chronozarr.spec_version = '0.3.7', /0.3.7.*unknown patch/],
]) {
  test(`reader decodes and notes ${label}`, async () => {
    const readable = buildSyntheticStore(spec);
    edit(readable, path, mutate);
    const store = await open(readable);
    assert.equal((await store.getCell(0, 0, 0, 1)).data[0], 1040);
    assert.ok(store.notes.some((text) => note.test(text)), JSON.stringify(store.notes));
    store.close();
  });
}

test('canonical metadata discovers levels and names with the optional reader mirrors absent', async () => {
  const readable = buildSyntheticStore({...spec, levelsMirror:false, consolidated:true});
  edit(readable, '/zarr.json', m => delete m.attributes.chronozarr.band_names);
  const store = await open(readable);
  assert.equal(readable.log.length, 1);
  assert.deepEqual(store.bands, ['B0', 'B1']);
  assert.equal(store.levels[1].resolution, 20);
});

// ---- CRS without an EPSG code (spec 0.4.0) ----

const rootAttrs = (readable) => JSON.parse(new TextDecoder().decode(readable.files.get('/zarr.json'))).attributes;

test('a 0.4.0 store whose arrays declare only proj:wkt2 opens: no code, no crs mirrors, same geometry and values', async () => {
  const readable = buildSyntheticStore({ ...spec, crs: { wkt2: MODIS_SINUSOIDAL.wkt2 } });
  assert.equal(rootAttrs(readable).chronozarr.spec_version, '0.4.0');
  assert.equal(rootAttrs(readable).chronozarr.crs, undefined, 'the fixture writes no mirror without a code');
  const store = await open(readable);
  assert.equal(store.crs, null);
  assert.equal(store.wkt2, MODIS_SINUSOIDAL.wkt2);
  assert.equal(store.projjson, null);
  assert.deepEqual(store.transform, [10, 0, 0, 0, -10, 0]);
  assert.deepEqual(store.levels[1].transform, [20, 0, 0, 0, -20, 0]);
  assert.equal((await store.getCell(0, 0, 0, 1)).data[0], 1040);
  assert.equal(store.hasMask, true);
});

test('proj:projjson alone is a CRS declaration', async () => {
  const store = await open(buildSyntheticStore({ ...spec, crs: { projjson: DAYMET_LCC.projjson } }));
  assert.equal(store.crs, null);
  assert.equal(store.wkt2, null);
  assert.deepEqual(store.projjson, DAYMET_LCC.projjson);
});

test('a code of another authority opens, with its crs mirror checked against it', async () => {
  const crs = { code: 'ESRI:54008', wkt2: MODIS_SINUSOIDAL.wkt2 };
  const store = await open(buildSyntheticStore({ ...spec, crs }));
  assert.equal(store.crs, 'ESRI:54008');
  assert.equal(store.wkt2, MODIS_SINUSOIDAL.wkt2);
  const readable = buildSyntheticStore({ ...spec, crs });
  edit(readable, '/zarr.json', (m) => (m.attributes.chronozarr.crs = 'ESRI:54009'));
  await assert.rejects(open(readable), /proj:code disagrees with chronozarr.crs/);
});

test('an EPSG store that also carries proj:wkt2 and proj:projjson reports all three', async () => {
  const crs = { code: WGS84_UTM_31N.code, wkt2: WGS84_UTM_31N.wkt2, projjson: { type: 'ProjectedCRS' } };
  const readable = buildSyntheticStore({ ...spec, crs });
  assert.equal(rootAttrs(readable).chronozarr.spec_version, '0.3.0');
  const store = await open(readable);
  assert.equal(store.crs, 'EPSG:32631');
  assert.equal(store.wkt2, WGS84_UTM_31N.wkt2);
  assert.deepEqual(store.projjson, { type: 'ProjectedCRS' });
});

for (const [label, path] of [['another level', '/1/data/zarr.json'], ['the mask', '/0/mask/zarr.json'], ['the coverage of another level', '/1/coverage/zarr.json']]) {
  test(`${label} declaring a different CRS than level 0 is refused`, async () => {
    const readable = buildSyntheticStore({ ...spec, crs: { wkt2: DAYMET_LCC.wkt2 } });
    edit(readable, path, (m) => (m.attributes['proj:wkt2'] = NSIDC_POLAR_STEREOGRAPHIC_NORTH.wkt2));
    await assert.rejects(open(readable), /CRS differs from level 0|auxiliary geometry, CRS or shape/);
    assert.ok(readable.log.every(({ key }) => key.endsWith('/zarr.json')), 'no measurements returned or fetched');
  });
}

test('equivalent redundant WKT and differently ordered PROJJSON do not change CRS identity', async () => {
  const readable = buildSyntheticStore({ ...spec, crs: WGS84_UTM_31N.code });
  edit(readable, '/1/data/zarr.json', (m) => { m.attributes['proj:wkt2'] = WGS84_UTM_31N.wkt2; });
  const store = await open(readable);
  assert.equal((await store.getCell(1, 0, 0, 1)).data[0], 1040);
  store.close();
  const jsonStore = buildSyntheticStore({ ...spec, crs: { projjson: DAYMET_LCC.projjson } });
  edit(jsonStore, '/1/data/zarr.json', (m) => { m.attributes['proj:projjson'] = Object.fromEntries(Object.entries(m.attributes['proj:projjson']).reverse()); });
  (await open(jsonStore)).close();
});

test('matching EPSG codes and root IDs do not hide different authoritative WKT parameters', async () => {
  const readable = buildSyntheticStore({ ...spec, crs: { code: WGS84_UTM_31N.code, wkt2: WGS84_UTM_31N.wkt2 } });
  edit(readable, '/1/data/zarr.json', (m) => { m.attributes['proj:wkt2'] = m.attributes['proj:wkt2'].replace('PARAMETER["False easting",500000', 'PARAMETER["False easting",600000'); });
  await assert.rejects(open(readable), /CRS differs/);
});

for (const field of ['wkt2', 'projjson']) test(`EPSG:4326 code and equivalent authoritative ${field} share CRS meaning`, async () => {
  assert.equal(await sameCrs({ crs: 'EPSG:4326' }, { [field]: WGS84_GEOGRAPHIC[field] }), true);
  const readable = buildSyntheticStore({ ...spec, crs: 'EPSG:4326' });
  edit(readable, '/1/data/zarr.json', (m) => { m.attributes[`proj:${field}`] = WGS84_GEOGRAPHIC[field]; });
  (await open(readable)).close();
});

for (const field of ['wkt2', 'projjson']) test(`EPSG:3857 code and equivalent authoritative ${field} share CRS meaning`, async () => {
  assert.equal(await sameCrs({ crs: 'EPSG:3857' }, { [field]: WGS84_PSEUDO_MERCATOR[field] }), true);
  const readable = buildSyntheticStore({ ...spec, crs: 'EPSG:3857' });
  edit(readable, '/1/data/zarr.json', (m) => { m.attributes[`proj:${field}`] = WGS84_PSEUDO_MERCATOR[field]; });
  (await open(readable)).close();
});

test('different geographic axes and angular units remain different despite identical authority IDs', async () => {
  const changedAxis = structuredClone(WGS84_GEOGRAPHIC.projjson);
  changedAxis.coordinate_system.axis[0].direction = 'south';
  const changedUnits = structuredClone(WGS84_GEOGRAPHIC.projjson);
  for (const axis of changedUnits.coordinate_system.axis) axis.unit = 'radian';
  const changedDatum = structuredClone(WGS84_GEOGRAPHIC.projjson);
  changedDatum.datum_ensemble.name = 'A different reference-frame ensemble';
  const changedMembers = structuredClone(WGS84_GEOGRAPHIC.projjson);
  changedMembers.datum_ensemble.members.pop();
  for (const projjson of [changedAxis, changedUnits, changedDatum, changedMembers]) assert.equal(await sameCrs({ crs: 'EPSG:4326' }, { projjson }), false);
  for (const [crs, fixture, unit] of [['EPSG:4326', WGS84_GEOGRAPHIC, 'radian'], ['EPSG:3857', WGS84_PSEUDO_MERCATOR, { type: 'LinearUnit', name: 'foot', conversion_factor: 0.3048 }]]) {
    for (const index of [0, 1]) {
      const projjson = structuredClone(fixture.projjson);
      projjson.coordinate_system.axis[index].unit = unit;
      assert.equal(await sameCrs({ crs }, { projjson }), false, `${crs} axis ${index} units matter independently`);
    }
  }
});

test('pseudo-Mercator retains the authoritative geodetic ellipsoid even though projection calculations use a sphere', async () => {
  const changedEllipsoid = structuredClone(WGS84_PSEUDO_MERCATOR.projjson);
  changedEllipsoid.base_crs.datum_ensemble.ellipsoid.inverse_flattening = 300;
  assert.equal(await sameCrs({ crs: 'EPSG:3857' }, { projjson: changedEllipsoid }), false);
});

test('WKT authority annotations with whitespace or optional children cannot hide contradictory methods', async () => {
  const changed = WGS84_PSEUDO_MERCATOR.wkt2.replace('Popular Visualisation Pseudo Mercator', 'Mercator (variant A)').replace('ID["EPSG",1024]', 'ID["EPSG",9804]');
  for (const wkt2 of [changed + '\n', changed.replace(/ID\["EPSG",3857\]\]$/, 'ID["EPSG",3857,URI["https://epsg.io/3857"]]]'), changed.replace(/\]$/, ',REMARK["after root ID"]]')]) {
    assert.equal(await sameCrs({ crs: 'EPSG:3857' }, { wkt2 }), false);
    const readable = buildSyntheticStore({ ...spec, crs: 'EPSG:3857' });
    edit(readable, '/1/data/zarr.json', (m) => { m.attributes['proj:wkt2'] = wkt2; });
    await assert.rejects(open(readable), /CRS differs/);
  }
});
