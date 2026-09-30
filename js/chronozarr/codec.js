// Decodes one compressed inner chunk (the bytes of a zarr chunk after sharding) into a typed array of the
// array's dtype. Used on the main thread (Node, tests) and inside decode workers. It takes the zarrita module
// as an argument so a worker can load it from a URL: workers do not see the page's import map.

import { DTYPES } from './metadata.js';

// Compressors zarrita's registry can load. blosc frames describe themselves, so cname (zstd, lz4), clevel and
// shuffle mode need no handling here.
const BYTES_TO_BYTES = new Set(['zstd', 'gzip', 'zlib', 'blosc', 'lz4']);
const WASM_CODECS = new Set(['zstd', 'blosc', 'lz4']);

/**
 * @param {typeof import('zarrita')} zarrita
 * @param {{dtype:string, shape:number[], codecs:Array<{name:string, configuration?:object}>}} spec
 *   codecs = the inner codec chain: `bytes` (little endian) followed by bytes-to-bytes compressors.
 * @returns {Promise<((bytes: Uint8Array) => Promise<Uint8Array|Uint16Array|Int16Array|Float32Array>) & {warmup: () => Promise<void>}>}
 */
export async function createChunkDecoder(zarrita, spec) {
  const dtype = DTYPES[spec.dtype];
  if (!dtype) throw new Error(`unsupported chunk dtype ${spec.dtype}, expected one of ${Object.keys(DTYPES)}`);
  const [bytesCodec, ...compressors] = spec.codecs;
  if (bytesCodec?.name !== 'bytes' || (dtype.bytes > 1 && (bytesCodec.configuration?.endian ?? 'little') !== 'little')) {
    throw new Error(`unsupported codec chain [${spec.codecs.map((c) => c.name)}]: expected a little-endian "bytes" codec first`);
  }
  if (new Uint8Array(new Uint16Array([1]).buffer)[0] !== 1) throw new Error('big-endian hosts are not supported');
  const elements = spec.shape.reduce((a, b) => a * b, 1);

  const chain = [];
  for (const { name, configuration } of compressors) {
    const load = BYTES_TO_BYTES.has(name) ? zarrita.registry.get(name) : undefined;
    if (!load) throw new Error(`unsupported codec "${name}" (supported: ${[...BYTES_TO_BYTES].join(', ')})`);
    const codec = (await load()).fromConfig(configuration ?? {}, { dataType: spec.dtype, shape: spec.shape, codecs: spec.codecs, fillValue: 0 });
    chain.push({ name, codec });
  }

  const decode = async (bytes) => {
    let data = bytes;
    for (let i = chain.length - 1; i >= 0; i--) data = await chain[i].codec.decode(data);
    if (data.byteLength !== elements * dtype.bytes) throw new Error(`decoded chunk is ${data.byteLength} bytes, expected ${elements * dtype.bytes}`);
    // Own the whole buffer so it can be transferred, and keep the alignment the typed array needs.
    if (data.byteOffset % dtype.bytes !== 0 || data.buffer.byteLength !== data.byteLength) {
      return new dtype.Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
    }
    return new dtype.Array(data.buffer, data.byteOffset, elements);
  };

  /** Instantiate WASM decoders ahead of the first real chunk. */
  decode.warmup = async () => {
    for (const { name, codec } of chain) {
      if (WASM_CODECS.has(name)) await codec.decode(await codec.encode(new Uint8Array(16)));
    }
  };
  return decode;
}
