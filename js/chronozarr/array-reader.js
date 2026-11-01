// General declared Zarr layouts. The ordinary one-chunk profile stays on the worker fast path.
import { createCodecPipeline } from '../vendor/zarrita/codecs.js';
import { DTYPES } from './metadata.js';
import { parseShardIndex, shardIndexRange } from './shard.js';

const product = (shape) => shape.reduce((a, b) => a * b, 1);

/** One reusable array handle, with one index per shard. Logical cells always return (band,y,x) C-order. */
export function createArrayReader(storage, path, readable, closeSignal) {
  const codec = createCodecPipeline({ dataType: storage.dtype, shape: storage.innerShape, codecs: storage.innerCodecs, fillValue: storage.fillValue });
  const indexes = new Map();
  const rank = storage.shape.length;
  const indexShape = storage.sharded ? storage.shardShape.map((n, i) => n / storage.innerShape[i]) : null;
  const nIndex = indexShape && product(indexShape);
  async function chunkBytes(coords, signal, priority, mutableIndexes) {
    if (!storage.sharded) return readable.get(`${path}/${storage.keyOf(coords)}`, { signal, priority });
    const shardCoords = coords.map((n, i) => Math.floor(n / indexShape[i]));
    const key = `${path}/${storage.keyOf(shardCoords)}`;
    // A partial trailing shard can be replaced by append. Without object version metadata, only its
    // current index can locate old values safely. Completed shards keep the session index cache.
    const mutable = storage.shape[0] % storage.shardShape[0] !== 0 && shardCoords[0] === Math.floor(storage.shape[0] / storage.shardShape[0]);
    const cache = mutable ? mutableIndexes : indexes;
    let index = cache.get(key);
    if (!index) {
      const handle = { value: priority.value };
      const promise = readable.getRange(key, shardIndexRange(nIndex, storage.indexHasCrc, storage.indexAtStart), { signal: closeSignal, priority: handle })
        .then((bytes) => bytes && parseShardIndex(bytes, nIndex, storage.indexHasCrc, key));
      index = { promise, priority: handle };
      cache.set(key, index);
      promise.catch(() => cache.delete(key));
    }
    index.priority.value = Math.min(index.priority.value, priority.value);
    const entries = await index.promise;
    if (!entries) return undefined;
    const linear = coords.reduce((n, coordinate, i) => n * indexShape[i] + coordinate % indexShape[i], 0);
    const offset = entries[2 * linear];
    if (offset < 0) return undefined;
    return readable.getRange(key, { offset, length: entries[2 * linear + 1] }, { signal, priority });
  }

  return async ({ t, row, col, chunkHeight, chunkWidth, nBand }, signal, priority) => {
    const spatial = [row * chunkHeight, col * chunkWidth];
    const start = rank === 4 ? [t, 0, ...spatial] : [t, ...spatial];
    const end = rank === 4 ? [t + 1, nBand, spatial[0] + chunkHeight, spatial[1] + chunkWidth] : [t + 1, spatial[0] + chunkHeight, spatial[1] + chunkWidth];
    end.forEach((n, i) => { end[i] = Math.min(n, storage.shape[i]); });
    const out = new DTYPES[storage.dtype].Array((rank === 4 ? nBand : 1) * chunkHeight * chunkWidth).fill(storage.fillValue);
    const chunks = [];
    const visit = (coords, axis) => {
      if (axis === rank) { chunks.push(coords); return; }
      for (let n = Math.floor(start[axis] / storage.innerShape[axis]); n <= Math.floor((end[axis] - 1) / storage.innerShape[axis]); n++) visit([...coords, n], axis + 1);
    };
    visit([], 0);
    let wireBytes = 0;
    const mutableIndexes = new Map();
    await Promise.all(chunks.map(async (coords) => {
      signal.throwIfAborted();
      const read = async () => {
        const bytes = await chunkBytes(coords, signal, priority, mutableIndexes);
        if (!bytes) return null;
        wireBytes += bytes.length;
        const decoded = await codec.decode(bytes);
        if (!(decoded.data instanceof DTYPES[storage.dtype].Array) || decoded.data.length !== product(storage.innerShape)) throw new Error(`${path}: decoded chunk does not match declared ${storage.dtype} shape ${storage.innerShape}`);
        return decoded;
      };
      let decoded;
      try { decoded = await read(); }
      catch (error) {
        if (!storage.sharded || signal.aborted) throw error;
        const shardKey = `${path}/${storage.keyOf(coords.map((n, i) => Math.floor(n / indexShape[i])))}`;
        indexes.delete(shardKey);
        mutableIndexes.delete(shardKey);
        // Re-read discovery metadata without adopting newer shapes: this array handle remains its opening snapshot.
        await Promise.all([readable.get('/zarr.json', { signal, priority, reload: true }), readable.get(`${path}/zarr.json`, { signal, priority, reload: true })]);
        decoded = await read();
      }
      if (!decoded) return;
      const origin = coords.map((n, i) => n * storage.innerShape[i]);
      const lo = origin.map((n, i) => Math.max(n, start[i]));
      const hi = origin.map((n, i) => Math.min(n + storage.innerShape[i], end[i]));
      for (let b = rank === 4 ? lo[1] : 0; b < (rank === 4 ? hi[1] : 1); b++) {
        for (let y = lo[rank - 2]; y < hi[rank - 2]; y++) {
          for (let x = lo[rank - 1]; x < hi[rank - 1]; x++) {
            const point = rank === 4 ? [t, b, y, x] : [t, y, x];
            const source = point.reduce((n, coordinate, i) => n + (coordinate - origin[i]) * decoded.stride[i], 0);
            out[b * chunkHeight * chunkWidth + (y - spatial[0]) * chunkWidth + x - spatial[1]] = decoded.data[source];
          }
        }
      }
    }));
    return { data: out, wireBytes };
  };
}
