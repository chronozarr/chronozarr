/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/scale_offset.js, sha256 of the published file 3defebbc44ad873a63d79176e08cbe4b384580342a1e439e28fb38346be46c0c
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
/*
The scale_offset codec is an array -> array codec that shifts and
scales input array values on the encoding path, and inverts this
transformation on the decoding path. This codec is only defined for
a specific set of data types (ints and floats). This codec preserves the data type
of the array.

The specification for this codec can be found at https://github.com/zarr-developers/zarr-extensions/tree/main/codecs/scale_offset
*/
import { InvalidMetadataError } from "../errors.js";
import { getCtr } from "../util.js";
import { unimplementedEncode } from "./_shared.js";
import { parseJsonScalar, } from "./json-scalar.js";
const SUPPORTED = new Set([
    "int8",
    "uint8",
    "int16",
    "uint16",
    "int32",
    "uint32",
    "int64",
    "uint64",
    "float16",
    "float32",
    "float64",
]);
export class ScaleOffsetCodec {
    kind = "array_to_array";
    #ctr;
    #scale;
    #offset;
    constructor(scale, offset, ctr) {
        this.#scale = scale;
        this.#offset = offset;
        this.#ctr = ctr;
    }
    static fromConfig(config, meta) {
        if (!SUPPORTED.has(meta.dataType)) {
            throw new InvalidMetadataError(`scale_offset codec does not support data type: ${meta.dataType}`);
        }
        return new ScaleOffsetCodec(parseJsonScalar(meta.dataType, config.scale ?? 1), parseJsonScalar(meta.dataType, config.offset ?? 0), getCtr(meta.dataType));
    }
    encode = unimplementedEncode("scale_offset");
    decode(chunk) {
        const src = chunk.data;
        const out = new this.#ctr(src.length);
        for (let i = 0; i < src.length; i++) {
            // @ts-expect-error - mix of bigint and number arithmetic is safe here
            out[i] = src[i] / this.#scale + this.#offset;
        }
        return { data: out, shape: chunk.shape, stride: chunk.stride };
    }
}
