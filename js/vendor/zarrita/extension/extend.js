/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/extension/extend.js, sha256 of the published file b6e12697c0b7ee9d5e0d7c891728ca39c3aaa5ad36f3c297b27ccbc9dc576e0d
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
/**
 * Walk a list of extensions, calling each synchronously until one returns a
 * `Promise`. From that point on, chain the remaining extensions with `.then`
 * so the caller only pays the cost of a Promise if any step actually needs
 * one. This is the shared runtime used by `extendStore` and `extendArray`.
 */
export function applyExtensions(value, extensions) {
    let result = value;
    for (let ext of extensions) {
        if (result instanceof Promise) {
            result = result.then((v) => ext(v));
        }
        else {
            result = ext(result);
        }
    }
    return result;
}
