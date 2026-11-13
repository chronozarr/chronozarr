// The browser remembers failed module imports by URL. Share each attempt within this page or worker, and use a
// different URL on the next attempt. A successful import stays cached and normal reads use the original URL.
function retryableCodec(path, load) {
  let attempt = 0;
  let loaded;
  return () => {
    loaded ??= (attempt === 0 ? load() : import(`${path}?retry=${attempt}`))
      .then((module) => module.default)
      .catch((error) => {
        attempt++;
        loaded = undefined;
        throw error;
      });
    return loaded;
  };
}

export const codecLoaders = new Map([
  ['zstd', retryableCodec('../vendor/numcodecs/zstd.js', () => import('../vendor/numcodecs/zstd.js'))],
  ['blosc', retryableCodec('../vendor/numcodecs/blosc.js', () => import('../vendor/numcodecs/blosc.js'))],
  ['lz4', retryableCodec('../vendor/numcodecs/lz4.js', () => import('../vendor/numcodecs/lz4.js'))],
]);
