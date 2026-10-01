// Zarr v3 sharding_indexed: shard index layout and crc32c.
// An index holds one (offset, nbytes) little-endian uint64 pair per inner chunk of the shard,
// optionally followed by a crc32c of those bytes. A pair of 2^64-1 marks an empty (fill value) chunk.

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
  for (let i = 0; i < bytes.length; i++) crc = CRC32C_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

const EMPTY = 0xffffffffffffffffn;

/**
 * A shard index that cannot be trusted: wrong length, failed checksum, or entries that point outside a shard of
 * the length it was read against. When the read used a `shard_bytes` length the reader takes this as a sign that
 * the length is stale (the store grew by an append) and recovers; otherwise it is a corrupt store.
 */
export class ShardIndexError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ShardIndexError';
  }
}

/** Encoded size of an index for `nChunks` inner chunks. */
export function indexByteLength(nChunks, hasCrc) {
  return 16 * nChunks + (hasCrc ? 4 : 0);
}

/**
 * Parse and verify a shard index. Returns a Float64Array [offset0, nbytes0, offset1, ...] with -1 for
 * empty chunks. Throws a ShardIndexError when the length or checksum is wrong (typically a wrong index_location,
 * or an index read with a stale shard length). With `shardBytes`, the length of the shard object the index was
 * read against, every chunk must also end inside it: the only check left when the index codecs carry no checksum.
 */
export function parseShardIndex(bytes, nChunks, hasCrc, where, shardBytes) {
  const expected = indexByteLength(nChunks, hasCrc);
  if (bytes.length !== expected) throw new ShardIndexError(`${where}: shard index is ${bytes.length} bytes, expected ${expected}`);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (hasCrc) {
    const stored = view.getUint32(16 * nChunks, true);
    const actual = crc32c(bytes.subarray(0, 16 * nChunks));
    if (stored !== actual) {
      throw new ShardIndexError(`${where}: shard index checksum mismatch (stored ${stored.toString(16)}, computed ${actual.toString(16)}); check index_location`);
    }
  }
  const entries = new Float64Array(2 * nChunks);
  for (let i = 0; i < nChunks; i++) {
    const offset = view.getBigUint64(16 * i, true);
    const length = view.getBigUint64(16 * i + 8, true);
    if (offset === EMPTY && length === EMPTY) {
      entries[2 * i] = -1;
      entries[2 * i + 1] = 0;
    } else {
      entries[2 * i] = Number(offset);
      entries[2 * i + 1] = Number(length);
      const end = entries[2 * i] + entries[2 * i + 1];
      if (shardBytes !== undefined && end > shardBytes) {
        throw new ShardIndexError(`${where}: shard index entry ${i} ends at byte ${end}, beyond the ${shardBytes}-byte shard it was read from`);
      }
    }
  }
  return entries;
}

/**
 * The read that fetches a shard's index: the first bytes for a start-located index; for an end-located one the
 * exact range when the shard's size is known (`shardBytes`, from the store's shard_bytes: no HEAD needed),
 * otherwise a suffix range.
 */
export function shardIndexRange(nChunks, hasCrc, atStart, shardBytes) {
  const length = indexByteLength(nChunks, hasCrc);
  if (atStart) return { offset: 0, length };
  if (Number.isInteger(shardBytes) && shardBytes >= length) return { offset: shardBytes - length, length };
  return { suffixLength: length };
}
