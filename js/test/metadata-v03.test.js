import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { CONVENTIONS } from '../chronozarr/metadata.js';
import { DAYMET_LCC, MODIS_SINUSOIDAL, NSIDC_POLAR_STEREOGRAPHIC_NORTH, WGS84_UTM_31N } from '../support/crs-fixtures.js';
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
  ['missing multiscales registration', '/zarr.json', m => m.attributes.zarr_conventions.pop(), /multiscales registration/],
  ['commit registration URL', '/0/data/zarr.json', m => m.attributes.zarr_conventions[0].spec_url = 'https://github.com/zarr-conventions/proj/blob/deadbeef/README.md', /literal v0.1/],
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
  ['grid disagreement', '/zarr.json', m => m.attributes.chronozarr.levels[0].grid = [99,99], /grid mirror/],
  ['plane geometry disagreement', '/0/mask/zarr.json', m => m.attributes['spatial:transform'][2] = 1, /auxiliary geometry/],
  ['missing dimension names', '/0/data/zarr.json', m => delete m.dimension_names, /dimension names/],
  ['non-square chunks', '/0/data/zarr.json', m => m.chunk_grid.configuration.chunk_shape[3] = 16, /square/],
  ['wrong key encoding', '/0/data/zarr.json', m => m.chunk_key_encoding.name = 'v2', /chunk_key_encoding/],
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
