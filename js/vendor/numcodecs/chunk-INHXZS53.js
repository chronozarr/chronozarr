/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: numcodecs 0.3.2, license MIT, https://github.com/manzt/numcodecs.js.git
 * file:    dist/chunk-INHXZS53.js, sha256 of the published file 73f3299e7103f9a9e0dfad5d24cd4076c5a995dbebaecda7f248b03c0df13bd9
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed. blosc.js, lz4.js and zstd.js embed WebAssembly builds of Blosc (with its bundled zlib and snappy), LZ4 and Zstandard; those C libraries carry their own permissive upstream licenses (BSD-style, zlib) and numcodecs ships no separate notice for them.
 */
var __toBinary = /* @__PURE__ */ (() => {
  var table = new Uint8Array(128);
  for (var i = 0; i < 64; i++)
    table[i < 26 ? i + 65 : i < 52 ? i + 71 : i < 62 ? i - 4 : i * 4 - 205] = i;
  return (base64) => {
    var n = base64.length, bytes = new Uint8Array((n - (base64[n - 1] == "=") - (base64[n - 2] == "=")) * 3 / 4 | 0);
    for (var i2 = 0, j = 0; i2 < n; ) {
      var c0 = table[base64.charCodeAt(i2++)], c1 = table[base64.charCodeAt(i2++)];
      var c2 = table[base64.charCodeAt(i2++)], c3 = table[base64.charCodeAt(i2++)];
      bytes[j++] = c0 << 2 | c1 >> 4;
      bytes[j++] = c1 << 4 | c2 >> 2;
      bytes[j++] = c2 << 6 | c3;
    }
    return bytes;
  };
})();

export {
  __toBinary
};
