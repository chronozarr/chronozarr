/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/gzip.js, sha256 of the published file 17117b26831e65d736025d33c090d3461e9d12eb9e9c65bc9d61152398e9a27c
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { decompress } from "../util.js";
import { unimplementedEncode } from "./_shared.js";
export class GzipCodec {
    kind = "bytes_to_bytes";
    static fromConfig(_) {
        return new GzipCodec();
    }
    encode = unimplementedEncode("gzip");
    async decode(bytes) {
        const buffer = await decompress(bytes, { format: "gzip" });
        return new Uint8Array(buffer);
    }
}
