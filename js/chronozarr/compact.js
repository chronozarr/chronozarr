// Lossless in-memory uint16 blocks. Each 128-value block stores its minimum,
// residual bit width and word offset; the GPU can read residuals directly.
// This changes neither store objects nor their values.
export const COMPACT_BLOCK_SIZE = 128;
export function packUint16(data) {
  if (!(data instanceof Uint16Array)) return null;
  const count = Math.ceil(data.length / COMPACT_BLOCK_SIZE);
  const blocks = new Uint32Array(count * 2);
  let wordCount = 0;
  for (let block = 0; block < count; block++) {
    const start = block * COMPACT_BLOCK_SIZE, end = Math.min(start + COMPACT_BLOCK_SIZE, data.length);
    let min = 65535, max = 0;
    for (let i = start; i < end; i++) { min = Math.min(min, data[i]); max = Math.max(max, data[i]); }
    const bits = 32 - Math.clz32(max - min);
    blocks[block * 2] = wordCount;
    blocks[block * 2 + 1] = min | (bits << 16);
    wordCount += Math.ceil((end - start) * bits / 32);
  }
  if ((wordCount + blocks.length) * 4 >= data.byteLength) return null;
  const words = new Uint32Array(wordCount);
  for (let block = 0; block < count; block++) {
    const start = block * COMPACT_BLOCK_SIZE, end = Math.min(start + COMPACT_BLOCK_SIZE, data.length);
    const base = blocks[block * 2], meta = blocks[block * 2 + 1], min = meta & 65535, bits = meta >>> 16;
    if (!bits) continue;
    for (let i = start; i < end; i++) {
      const bit = (i - start) * bits, word = base + (bit >>> 5), shift = bit & 31, value = data[i] - min;
      words[word] |= value << shift;
      if (shift + bits > 32) words[word + 1] |= value >>> (32 - shift);
    }
  }
  return restoreCompactUint16({ words, blocks, length: data.length });
}
export function unpackUint16(packed) {
  const data = new Uint16Array(packed.length);
  for (let block = 0; block < packed.blocks.length / 2; block++) {
    const start = block * COMPACT_BLOCK_SIZE, end = Math.min(start + COMPACT_BLOCK_SIZE, data.length);
    const base = packed.blocks[block * 2], meta = packed.blocks[block * 2 + 1], min = meta & 65535, bits = meta >>> 16;
    if (!bits) { data.fill(min, start, end); continue; }
    const mask = (1 << bits) - 1;
    for (let i = start; i < end; i++) {
      const bit = (i - start) * bits, word = base + (bit >>> 5), shift = bit & 31;
      let value = packed.words[word] >>> shift;
      if (shift + bits > 32) value |= packed.words[word + 1] << (32 - shift);
      data[i] = min + (value & mask);
    }
  }
  return data;
}

/** Exact random access without materializing a whole chunk. */
export function compactUint16At(packed, index) {
  const block = index >>> 7, meta = packed.blocks[block * 2 + 1], min = meta & 65535, bits = meta >>> 16;
  if (!bits) return min;
  const bit = (index & 127) * bits, word = packed.blocks[block * 2] + (bit >>> 5), shift = bit & 31;
  let value = packed.words[word] >>> shift;
  if (shift + bits > 32) value |= packed.words[word + 1] << (32 - shift);
  return min + (value & ((1 << bits) - 1));
}

/** Restore methods after the two backing buffers cross a worker boundary. */
export function restoreCompactUint16({ words, blocks, length }) {
  return { words, blocks, length, byteLength: words.byteLength + blocks.byteLength, unpack() { return unpackUint16(this); } };
}
