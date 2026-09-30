/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/zlib.js, sha256 of the published file 488c10f063789501e621b22619721ecf6e3fc2a5243f40942059547d204f1032
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { decompress } from "../util.js";
import { unimplementedEncode } from "./_shared.js";
export class ZlibCodec {
    kind = "bytes_to_bytes";
    static fromConfig(_) {
        return new ZlibCodec();
    }
    encode = unimplementedEncode("zlib");
    async decode(bytes) {
        const buffer = await decompress(bytes, { format: "deflate" });
        return new Uint8Array(buffer);
    }
}
