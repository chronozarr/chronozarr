/** Keep ordinary timelines raw; losslessly pack oversized uint16 timelines in RAM.
 * Run before configuring GPU pools so every renderer uses the same representation. */
import { compactTextureShape } from './renderer.js';

export function prepareMovieCache(store, maxTextureSize = Infinity) {
  const native = store.levels[0];
  const { wordHeight, blockWidth, blockHeight } = compactTextureShape(native.nBand, native.chunkWidth, native.chunkHeight);
  const fitsGpu = Math.max(native.chunkWidth, native.chunkHeight, wordHeight, blockWidth, blockHeight) <= maxTextureSize;
  const loopBytes = native.gridRows * native.gridCols * native.nTime * native.chunkBytes;
  if (store.dtype === 'uint16' && fitsGpu && loopBytes > store.budgets().totalBytes * 0.9) store.enableCompactCache();
  return store.compactCacheEnabled;
}
