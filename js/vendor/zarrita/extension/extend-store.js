/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/extension/extend-store.js, sha256 of the published file 12ec53ee14c97c02149e8889ad50f60ea9ac38b7884ff158f543168555d33738
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { applyExtensions } from "./extend.js";
export function extendStore(store, ...extensions) {
    return applyExtensions(store, extensions);
}
