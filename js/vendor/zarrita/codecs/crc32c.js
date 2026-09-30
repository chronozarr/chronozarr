/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/crc32c.js, sha256 of the published file a9092b9dd36a8e577c716fde65920b9e63fbc10fada4ea0b50c709c31af76c22
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { unimplementedEncode } from "./_shared.js";
export class Crc32cCodec {
    kind = "bytes_to_bytes";
    static fromConfig() {
        return new Crc32cCodec();
    }
    encode = unimplementedEncode("crc32c");
    decode(arr) {
        return new Uint8Array(arr.buffer, arr.byteOffset, arr.byteLength - 4);
    }
    computeEncodedSize(decodedSize) {
        return decodedSize + 4;
    }
}
