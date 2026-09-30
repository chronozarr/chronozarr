// Parsing of chronozarr root attributes and Zarr v3 array metadata into the shapes the reader uses.
// No I/O: openStore fetches the JSON, this module validates and normalizes it.

export const DTYPES = {
  uint8: { Array: Uint8Array, bytes: 1 },
  uint16: { Array: Uint16Array, bytes: 2 },
  int16: { Array: Int16Array, bytes: 2 },
  float32: { Array: Float32Array, bytes: 4 },
};

const SUPPORTED_SPEC = /^0\.[12]\./;
/** Sentinel-2 L2A reflectance scale, implied by the string form of `bands` in v0.1 stores. */
const LEGACY_BAND_SCALE = 1e-4;

export function requireStore(condition, baseUrl, message) {
  if (!condition) throw new Error(`${baseUrl}: not a valid chronozarr store: ${message}`);
}

/** The `chronozarr` root block, checked for version and temporal encoding. */
export function parseRoot(root, baseUrl) {
  const cz = root.attributes?.chronozarr;
  requireStore(cz, baseUrl, 'root attributes have no "chronozarr" entry');
  requireStore(SUPPORTED_SPEC.test(String(cz.spec_version)), baseUrl, `unsupported spec_version ${cz.spec_version}`);
  requireStore(cz.temporal?.encoding === 'star-delta' || cz.temporal?.encoding === 'none', baseUrl, `unsupported temporal encoding ${cz.temporal?.encoding}`);
  requireStore(Array.isArray(cz.times) && cz.times.length > 0, baseUrl, 'chronozarr.times is missing');
  const datasets = root.attributes.multiscales?.[0]?.datasets;
  requireStore(Array.isArray(datasets) && datasets.length > 0, baseUrl, 'multiscales[0].datasets is missing');
  return { cz, datasets };
}

/**
 * Band objects {name, common_name?, scale, offset, units?} from either form of `bands`. Objects carry their
 * own scale and offset (defaults 1 and 0); a v0.1 string list implies Sentinel-2 reflectance (scale 1e-4).
 */
export function normalizeBands(cz, baseUrl) {
  requireStore(Array.isArray(cz.bands) && cz.bands.length > 0, baseUrl, 'chronozarr.bands is missing');
  const legacy = /^0\.1\./.test(String(cz.spec_version));
  const bands = cz.bands.map((band) => {
    if (typeof band === 'string') return { name: band, scale: legacy ? LEGACY_BAND_SCALE : 1, offset: 0 };
    requireStore(typeof band?.name === 'string', baseUrl, `band ${JSON.stringify(band)} has no name`);
    return { ...band, scale: band.scale ?? 1, offset: band.offset ?? 0 };
  });
  return { bands, bandNames: cz.band_names ?? bands.map((b) => b.name) };
}

function parseFillValue(value, dtype) {
  if (value === 'NaN') return NaN;
  if (value === 'Infinity') return Infinity;
  if (value === '-Infinity') return -Infinity;
  const n = Number(value ?? 0);
  requireStore(Number.isFinite(n) || dtype === 'float32', 'array', `fill_value ${value} is not a number`);
  return n;
}

/**
 * How one array (the data array, or a mask/coverage variable) is laid out, from its zarr.json.
 * `rank` is 4 for (time, band, y, x) and 3 for (time, y, x). Inner chunks always span one timestep.
 */
export function parseStorage(meta, { path, rank, baseUrl }) {
  const where = `${baseUrl}: ${path}`;
  requireStore(meta.zarr_format === 3 && meta.node_type === 'array', baseUrl, `${path}/zarr.json is not a Zarr v3 array`);
  requireStore(DTYPES[meta.data_type], baseUrl, `${where} dtype is ${meta.data_type}, expected one of ${Object.keys(DTYPES)}`);
  requireStore(meta.shape?.length === rank, baseUrl, `${where} has ${meta.shape?.length} dimensions, expected ${rank}`);
  requireStore(meta.chunk_grid?.name === 'regular', baseUrl, `${where} chunk grid ${meta.chunk_grid?.name} is not supported`);
  const encoding = meta.chunk_key_encoding ?? { name: 'default' };
  requireStore(encoding.name === 'default' || encoding.name === 'v2', baseUrl, `${where} chunk_key_encoding ${encoding.name} is not supported`);
  const separator = encoding.configuration?.separator ?? (encoding.name === 'v2' ? '.' : '/');
  const storage = {
    dtype: meta.data_type,
    fillValue: parseFillValue(meta.fill_value, meta.data_type),
    keyOf: (coords) => (encoding.name === 'default' ? ['c', ...coords] : coords).join(separator),
  };
  const gridShape = meta.chunk_grid.configuration.chunk_shape;
  const sharding = meta.codecs.find((c) => c.name === 'sharding_indexed');
  if (!sharding) {
    requireStore(gridShape[0] === 1, baseUrl, `${where} unsharded chunks must span one timestep, got ${gridShape}`);
    return { ...storage, sharded: false, shardTime: 1, innerShape: gridShape, innerCodecs: meta.codecs };
  }
  const cfg = sharding.configuration;
  const indexCodecs = cfg.index_codecs.map((c) => c.name);
  requireStore(
    (indexCodecs.length === 1 && indexCodecs[0] === 'bytes') || (indexCodecs.length === 2 && indexCodecs[0] === 'bytes' && indexCodecs[1] === 'crc32c'),
    baseUrl,
    `${where} index_codecs [${indexCodecs}] are not supported`,
  );
  const inner = cfg.chunk_shape;
  const sameSpace = gridShape.slice(1).every((n, i) => n === inner[i + 1]);
  requireStore(inner[0] === 1 && sameSpace, baseUrl, `${where} shard shape (${gridShape}) must be (shard_time, ...) over inner chunks (${inner}) that span one timestep`);
  return {
    ...storage,
    sharded: true,
    shardTime: gridShape[0],
    innerShape: inner,
    innerCodecs: cfg.codecs,
    indexAtStart: (cfg.index_location ?? 'end') === 'start',
    indexHasCrc: indexCodecs.includes('crc32c'),
  };
}

/** What `decode` needs for one array: the typed inner chunk and its compression chain. `key` identifies it to workers. */
export function decodeSpec(storage) {
  const spec = { dtype: storage.dtype, shape: storage.innerShape, codecs: storage.innerCodecs };
  return { key: JSON.stringify(spec), ...spec };
}
