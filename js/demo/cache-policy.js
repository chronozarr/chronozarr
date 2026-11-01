/** Keep ordinary timelines raw; losslessly pack oversized uint16 timelines in RAM.
 * Run before configuring GPU pools so every renderer uses the same representation. */
export function prepareMovieCache(store) {
  const native = store.levels[0];
  const loopBytes = native.gridRows * native.gridCols * native.nTime * native.chunkBytes;
  if (store.dtype === 'uint16' && loopBytes > store.budgets().totalBytes * 0.9) store.enableCompactCache();
  return store.compactCacheEnabled;
}
