// Module worker: decodes compressed chunks off the main thread. It serves any array of any store: each decode
// message carries the chunk spec, and the codec chain for a spec is built once and kept (a failed build is tried again).
//   -> {type:'init', zarritaUrl, warm?}     <- {type:'ready'} | {type:'error', message}
//        `warm`: specs whose codecs are instantiated before 'ready', so a worker that is ready can decode at once
//   -> {type:'warm', spec}                  (instantiate the spec's codecs early; no reply)
//   -> {type:'decode', id, spec, bytes, compact?} <- {type:'decoded', id, data} | {type:'error', id, message}
// Replies transfer either the decoded typed array or just packed words and block metadata.

import { createChunkDecoder } from './codec.js';
import { codecLoaders } from './codec-loaders.js';
import { packUint16 } from './compact.js';

const builtinRegistryUrl = new URL('../vendor/zarrita/codecs.js', import.meta.url).href;
let zarrita = null;
const decoders = new Map();

function decoderFor(spec) {
  const { key } = spec;
  let decoder = decoders.get(key);
  if (!decoder) {
    decoder = createChunkDecoder(zarrita, spec);
    // A load that failed is forgotten, so the next message that needs the spec loads it again. The decode that
    // awaits this promise still reports the error.
    decoder.catch(() => {
      if (decoders.get(key) === decoder) decoders.delete(key);
    });
    decoders.set(key, decoder);
  }
  return decoder;
}

self.onmessage = async ({ data: message }) => {
  try {
    if (message.type === 'init') {
      zarrita = await import(message.zarritaUrl);
      if (message.zarritaUrl === builtinRegistryUrl) zarrita = { registry: zarrita.registry, codecLoaders };
      // Best effort, as with a 'warm' message. Not ready until the codecs are: a job given to a worker that is still
      // fetching WebAssembly waits for it, while the pool's fallback decoder (a warm one) could have run the job.
      await Promise.all((message.warm ?? []).map(async (spec) => (await decoderFor(spec)).warmup())).catch(() => {});
      self.postMessage({ type: 'ready' });
    } else if (message.type === 'warm') {
      // Best effort: a decode of the same spec reports the real error.
      await (await decoderFor(message.spec)).warmup().catch(() => {});
    } else {
      const data = await (await decoderFor(message.spec))(message.bytes);
      const packed = message.compact ? packUint16(data) : null;
      if (packed) {
        const { words, blocks, length, byteLength } = packed;
        self.postMessage({ type: 'decoded', id: message.id, data: { words, blocks, length, byteLength } }, [words.buffer, blocks.buffer]);
      } else {
        self.postMessage({ type: 'decoded', id: message.id, data }, [data.buffer]);
      }
    }
  } catch (error) {
    if (message.type !== 'warm') self.postMessage({ type: 'error', id: message.id, message: error.message });
  }
};
