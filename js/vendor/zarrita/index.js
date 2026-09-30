/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/index.js, sha256 of the published file 8a6cb46887e37ea973e508ebc3b7eb047f6e87f55965ae0b702d09bf3407da7e
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
/**
 * A minimal, modular Zarr implementation for JavaScript.
 *
 * Zarrita reads and writes chunked, n-dimensional arrays backed by pluggable
 * stores (HTTP, filesystem, in-memory, ...) and supports both Zarr v2 and v3
 * on-disk formats. The API is deliberately small: open an {@linkcode Array}
 * or {@linkcode Group} from a store, then read or write chunks with
 * {@linkcode get} and {@linkcode set}.
 *
 * ```ts
 * import * as zarr from "zarrita";
 *
 * const store = new zarr.FetchStore("https://example.com/data.zarr");
 * const arr = await zarr.open(store, { kind: "array" });
 *
 * // Read a 2D sub-region into an ndarray-like view.
 * const region = await zarr.get(arr, [zarr.slice(0, 10), null]);
 * console.log(region.shape, region.data);
 * ```
 *
 * See the {@link https://manzt.github.io/zarrita.js/ | cookbook} for more
 * recipes (creation, consolidated metadata, store extensions, slicing).
 *
 * @module
 */
// re-export fetch store from storage
export { default as FetchStore } from "../zarrita-storage/fetch.js";
// core
export { registry } from "./codecs.js";
export { create } from "./create.js";
export { CodecPipelineError, InvalidMetadataError, InvalidSelectionError, isZarritaError, NotFoundError, UnknownCodecError, UnsupportedError, } from "./errors.js";
export { withByteCaching, } from "./extension/caching.js";
export { withConsolidatedMetadata, withMaybeConsolidatedMetadata, } from "./extension/consolidation.js";
export { defineStoreExtension } from "./extension/define.js";
export { defineArrayExtension, } from "./extension/define-array.js";
export { extendArray } from "./extension/extend-array.js";
export { extendStore } from "./extension/extend-store.js";
export { withRangeCoalescing, } from "./extension/range-coalescing.js";
export { Array, Group, Location, root } from "./hierarchy.js";
// internal exports for @zarrita/ndarray
export { get as _zarrita_internal_get } from "./indexing/get.js";
export { get, set } from "./indexing/ops.js";
export { set as _zarrita_internal_set } from "./indexing/set.js";
export { select, slice, sliceIndices as _zarrita_internal_sliceIndices, } from "./indexing/util.js";
export { open } from "./open.js";
export { BoolArray, ByteStringArray, UnicodeStringArray, } from "./typedarray.js";
export { getStrides as _zarrita_internal_getStrides } from "./util.js";
