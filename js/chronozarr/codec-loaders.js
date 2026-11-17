function retryCodecImport(load, attempt) {
  // A split bundle can rewrite the literal import to a generated chunk. Inspect the loader only on retry.
  const source = Function.prototype.toString.call(load);
  const match = /^\s*\(\s*\)\s*=>\s*import\s*\(\s*(?:"([^"\\]+)"|'([^'\\]+)')\s*\)\s*$/.exec(source);
  const specifier = match?.[1] ?? match?.[2];
  // If a bundler replaced the native import with its runtime, let that runtime retry.
  if (!specifier || !/^(?:\.{0,2}\/|https?:\/\/|file:\/\/)/.test(specifier)) return load();

  let url;
  try {
    url = new URL(specifier, import.meta.url);
  } catch {
    return load();
  }
  // Keep the original query encoding and insert the new parameter before any fragment.
  url.search += `${url.search ? '&' : '?'}retry=${attempt}`;
  return import(url.href);
}

// The browser remembers failed module imports by URL. Share each attempt within this page or worker, and use a
// different URL on the next attempt. A successful import stays cached and normal reads use the original URL.
function retryableCodec(load) {
  let attempt = 0;
  let loaded;
  return () => {
    loaded ??= (attempt === 0 ? load() : retryCodecImport(load, attempt))
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
  ['zstd', retryableCodec(() => import('../vendor/numcodecs/zstd.js'))],
  ['blosc', retryableCodec(() => import('../vendor/numcodecs/blosc.js'))],
  ['lz4', retryableCodec(() => import('../vendor/numcodecs/lz4.js'))],
]);
