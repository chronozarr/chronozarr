// Module worker: decodes compressed chunks off the main thread. It serves any array of any store: each decode
// message carries the chunk spec, and the codec chain for a spec is built once and kept.
//   -> {type:'init', zarritaUrl}            <- {type:'ready'} | {type:'error', message}
//   -> {type:'warm', spec}                  (instantiate the spec's codecs early; no reply)
//   -> {type:'decode', id, spec, bytes}     <- {type:'decoded', id, data} (typed array, transferred) | {type:'error', id, message}

import { createChunkDecoder } from './codec.js';

let zarrita = null;
const decoders = new Map();

function decoderFor(spec) {
  let decoder = decoders.get(spec.key);
  if (!decoder) {
    decoder = createChunkDecoder(zarrita, spec);
    decoders.set(spec.key, decoder);
  }
  return decoder;
}

self.onmessage = async ({ data: message }) => {
  try {
    if (message.type === 'init') {
      zarrita = await import(message.zarritaUrl);
      self.postMessage({ type: 'ready' });
    } else if (message.type === 'warm') {
      // Best effort: a decode of the same spec reports the real error.
      await (await decoderFor(message.spec)).warmup().catch(() => {});
    } else {
      const data = await (await decoderFor(message.spec))(message.bytes);
      self.postMessage({ type: 'decoded', id: message.id, data }, [data.buffer]);
    }
  } catch (error) {
    if (message.type !== 'warm') self.postMessage({ type: 'error', id: message.id, message: error.message });
  }
};
