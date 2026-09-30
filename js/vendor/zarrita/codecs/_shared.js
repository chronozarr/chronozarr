/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/_shared.js, sha256 of the published file e3909af7aee96bb18c307ffff40519aa2d0edc74741e580a15a86db4f4a40807
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { UnsupportedError } from "../errors.js";
export function unimplementedEncode(codecName) {
    return () => {
        throw new UnsupportedError(`${codecName} encode`);
    };
}
