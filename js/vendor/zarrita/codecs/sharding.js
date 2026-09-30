/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/codecs/sharding.js, sha256 of the published file 4ab25f30078d9be06b6c2e96efca94ab6d8ea404092d4ba06c82f0b67f9d80aa
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { createCodecPipeline } from "../codecs.js";
import { UnsupportedError } from "../errors.js";
const MAX_BIG_UINT = 18446744073709551615n;
export function createShardedChunkGetter(location, shardShape, encodeShardKey, shardingConfig) {
    if (!location.store.getRange) {
        throw new UnsupportedError("sharding requires a store with getRange");
    }
    let getRange = location.store.getRange.bind(location.store);
    let indexShape = shardShape.map((d, i) => d / shardingConfig.chunk_shape[i]);
    let indexCodec = createCodecPipeline({
        dataType: "uint64",
        shape: [...indexShape, 2],
        codecs: shardingConfig.index_codecs,
        fillValue: null,
    });
    // The shard index is a uint64 array with shape [...indexShape, 2]
    // (offset, length) per inner chunk. Its on-disk size depends on the
    // index_codecs pipeline — e.g. crc32c appends 4 bytes — so we ask the
    // pipeline rather than hardcoding any constant.
    let rawIndexSize = 16 * indexShape.reduce((a, b) => a * b, 1);
    let cache = {};
    return async (chunkCoord, options) => {
        let shardCoord = chunkCoord.map((d, i) => Math.floor(d / indexShape[i]));
        let shardPath = location.resolve(encodeShardKey(shardCoord)).path;
        if (!(shardPath in cache)) {
            cache[shardPath] = (async () => {
                let suffixLength = await indexCodec.computeEncodedSize(rawIndexSize);
                let bytes = await getRange(shardPath, { suffixLength }, options);
                return bytes ? await indexCodec.decode(bytes) : null;
            })().catch((err) => {
                delete cache[shardPath];
                throw err;
            });
        }
        let index = await cache[shardPath];
        if (index === null) {
            return undefined;
        }
        let { data, shape, stride } = index;
        let linearOffset = chunkCoord
            .map((d, i) => d % shape[i])
            .reduce((acc, sel, idx) => acc + sel * stride[idx], 0);
        let offset = data[linearOffset];
        let length = data[linearOffset + 1];
        // write null chunk when 2^64-1 indicates fill value
        if (offset === MAX_BIG_UINT && length === MAX_BIG_UINT) {
            return undefined;
        }
        return getRange(shardPath, {
            offset: Number(offset),
            length: Number(length),
        }, options);
    };
}
