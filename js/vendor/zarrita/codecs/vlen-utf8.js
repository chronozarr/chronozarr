/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/vlen-utf8.js, sha256 of the published file 322218b2f792b89c453273bd94726e680e67016da8c13dde547c498b0cedd699
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { getStrides } from "../util.js";
import { unimplementedEncode } from "./_shared.js";
export class VLenUTF8 {
    kind = "array_to_bytes";
    #shape;
    #strides;
    constructor(shape) {
        this.#shape = shape;
        this.#strides = getStrides(shape, "C");
    }
    static fromConfig(_, meta) {
        return new VLenUTF8(meta.shape);
    }
    encode = unimplementedEncode("vlen-utf8");
    decode(bytes) {
        let decoder = new TextDecoder();
        let view = new DataView(bytes.buffer);
        let data = Array(view.getUint32(0, true));
        let pos = 4;
        for (let i = 0; i < data.length; i++) {
            let itemLength = view.getUint32(pos, true);
            pos += 4;
            data[i] = decoder.decode(bytes.buffer.slice(pos, pos + itemLength));
            pos += itemLength;
        }
        return { data, shape: this.#shape, stride: this.#strides };
    }
}
