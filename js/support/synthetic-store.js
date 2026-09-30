// In-memory chronozarr v0.1 store built in JS, independent of the Python writer.
// Sharded (uncompressed inner chunks, crc32c index at "start" or "end") or unsharded.
// Implements the zarrita AsyncReadable interface and logs every call.

const CRC32C_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0x82f63b78 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32c(bytes) {
  let crc = 0xffffffff;
  for (const b of bytes) crc = CRC32C_TABLE[(crc ^ b) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

const json = (obj) => new TextEncoder().encode(JSON.stringify(obj));

/** Deterministic source value for (t, band, y, x): smooth in t so int16 residuals never overflow. */
export function sourceValue(t, b, y, x) {
  return 1000 + b * 500 + ((y * 7 + x * 13) % 2000) + t * 37 + (x % 5 === 0 ? t * 3 : 0);
}

/**
 * @param {{nTime:number, nBand:number, height:number, width:number, chunk:number, anchorInterval:number,
 *   sharded:boolean, indexLocation?:'start'|'end', bands?:string[]}} spec
 */
export function buildSyntheticStore(spec) {
  const { nTime, nBand, height, width, chunk, anchorInterval, sharded, indexLocation = 'end' } = spec;
  const bands = spec.bands ?? Array.from({ length: nBand }, (_, i) => `B${i}`);
  const anchors = [];
  const deltaReference = {};
  for (let t = 0; t < nTime; t++) {
    if (t % anchorInterval === 0) anchors.push(t);
    else deltaReference[t] = t - (t % anchorInterval);
  }
  const files = new Map();
  const gridRows = Math.ceil(height / chunk);
  const gridCols = Math.ceil(width / chunk);
  const chunkShape = [1, nBand, chunk, chunk];
  const bytesCodec = { name: 'bytes', configuration: { endian: 'little' } };

  const encodeChunk = (t, row, col) => {
    const out = new Uint16Array(nBand * chunk * chunk);
    const anchorT = t in deltaReference ? deltaReference[t] : t;
    for (let b = 0; b < nBand; b++) {
      for (let y = 0; y < chunk; y++) {
        for (let x = 0; x < chunk; x++) {
          const gy = row * chunk + y;
          const gx = col * chunk + x;
          if (gy >= height || gx >= width) continue;
          const value = sourceValue(t, b, gy, gx);
          const stored = anchorT === t ? value : (value - sourceValue(anchorT, b, gy, gx)) & 0xffff;
          out[(b * chunk + y) * chunk + x] = stored;
        }
      }
    }
    return new Uint8Array(out.buffer);
  };

  const arrayMeta = {
    zarr_format: 3,
    node_type: 'array',
    shape: [nTime, nBand, height, width],
    data_type: 'uint16',
    chunk_grid: { name: 'regular', configuration: { chunk_shape: sharded ? [nTime, nBand, chunk, chunk] : chunkShape } },
    chunk_key_encoding: { name: 'default', configuration: { separator: '/' } },
    fill_value: 0,
    codecs: sharded
      ? [
          {
            name: 'sharding_indexed',
            configuration: {
              chunk_shape: chunkShape,
              codecs: [bytesCodec],
              index_codecs: [bytesCodec, { name: 'crc32c' }],
              index_location: indexLocation,
            },
          },
        ]
      : [bytesCodec],
    dimension_names: ['time', 'band', 'y', 'x'],
    attributes: {},
  };
  files.set('/0/data/zarr.json', json(arrayMeta));
  files.set('/0/zarr.json', json({ zarr_format: 3, node_type: 'group', attributes: {} }));
  files.set(
    '/zarr.json',
    json({
      zarr_format: 3,
      node_type: 'group',
      attributes: {
        multiscales: [{ datasets: [{ path: '0', pixels_per_tile: chunk, crs: 'EPSG:32631' }], type: 'reduce' }],
        chronozarr: {
          spec_version: '0.1.0',
          variable: 'data',
          times: Array.from({ length: nTime }, (_, t) => `2024-01-${String(t + 1).padStart(2, '0')}T00:00:00Z`),
          bands,
          nodata: 0,
          crs: 'EPSG:32631',
          temporal: { encoding: 'star-delta', anchor_interval: anchorInterval, anchor_indices: anchors, delta_reference: deltaReference },
        },
      },
    }),
  );

  for (let row = 0; row < gridRows; row++) {
    for (let col = 0; col < gridCols; col++) {
      const chunks = Array.from({ length: nTime }, (_, t) => encodeChunk(t, row, col));
      if (!sharded) {
        chunks.forEach((bytes, t) => files.set(`/0/data/c/${t}/0/${row}/${col}`, bytes));
        continue;
      }
      const indexBytes = 16 * nTime + 4;
      let offset = indexLocation === 'start' ? indexBytes : 0;
      const index = new DataView(new ArrayBuffer(16 * nTime));
      chunks.forEach((bytes, t) => {
        index.setBigUint64(16 * t, BigInt(offset), true);
        index.setBigUint64(16 * t + 8, BigInt(bytes.length), true);
        offset += bytes.length;
      });
      const indexRaw = new Uint8Array(index.buffer);
      const indexWithCrc = new Uint8Array(indexBytes);
      indexWithCrc.set(indexRaw);
      new DataView(indexWithCrc.buffer).setUint32(indexRaw.length, crc32c(indexRaw), true);
      const parts = indexLocation === 'start' ? [indexWithCrc, ...chunks] : [...chunks, indexWithCrc];
      const shard = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
      let at = 0;
      for (const part of parts) {
        shard.set(part, at);
        at += part.length;
      }
      files.set(`/0/data/c/0/0/${row}/${col}`, shard);
    }
  }

  return new SyntheticReadable(files, spec.delayMs ?? 0);
}

class SyntheticReadable {
  constructor(files, delayMs) {
    this.files = files;
    this.delayMs = delayMs;
    this.log = [];
  }

  async #respond(key, range, options, read) {
    this.log.push({ key, range, signal: options?.signal ?? null });
    if (this.delayMs > 0) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, this.delayMs);
        options?.signal?.addEventListener('abort', () => {
          clearTimeout(timer);
          reject(new DOMException('Aborted', 'AbortError'));
        });
      });
    }
    if (options?.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    return read(this.files.get(key));
  }

  get(key, options) {
    return this.#respond(key, null, options, (file) => file);
  }

  getRange(key, range, options) {
    return this.#respond(key, range, options, (file) => {
      if (!file) return undefined;
      if ('suffixLength' in range) return file.slice(file.length - range.suffixLength);
      return file.slice(range.offset, range.offset + range.length);
    });
  }
}
