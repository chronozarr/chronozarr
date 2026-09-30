// Module worker: decodes compressed chunks off the main thread.
//   -> {type:'init', zarritaUrl, spec}     <- {type:'ready'} | {type:'error', message}
//   -> {type:'decode', id, bytes}          <- {type:'decoded', id, data} (Uint16Array, transferred) | {type:'error', id, message}

import { createChunkDecoder } from './codec.js';

let decode = null;

self.onmessage = async ({ data: message }) => {
  try {
    if (message.type === 'init') {
      const zarrita = await import(message.zarritaUrl);
      decode = await createChunkDecoder(zarrita, message.spec);
      await decode.warmup();
      self.postMessage({ type: 'ready' });
    } else {
      const data = await decode(message.bytes);
      self.postMessage({ type: 'decoded', id: message.id, data }, [data.buffer]);
    }
  } catch (error) {
    self.postMessage({ type: 'error', id: message.id, message: error.message });
  }
};
