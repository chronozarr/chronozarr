/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/create.js, sha256 of the published file b7a2c1a2f8252db63fe5e2ace28b346fce3fc94d34876667823b6b215aee3acf
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { Array, Group, Location } from "./hierarchy.js";
import { jsonEncodeObject } from "./util.js";
export async function create(location, options = {}) {
    let loc = "store" in location ? location : new Location(location);
    if ("shape" in options) {
        let arr = await createArray(loc, options);
        return arr;
    }
    return createGroup(loc, options);
}
async function createGroup(location, options = {}) {
    let metadata = {
        zarr_format: 3,
        node_type: "group",
        attributes: options.attributes ?? {},
    };
    await location.store.set(location.resolve("zarr.json").path, jsonEncodeObject(metadata));
    return new Group(location.store, location.path, metadata);
}
async function createArray(location, options) {
    let metadata = {
        zarr_format: 3,
        node_type: "array",
        shape: options.shape,
        data_type: options.dtype,
        chunk_grid: {
            name: "regular",
            configuration: {
                chunk_shape: options.chunkShape,
            },
        },
        chunk_key_encoding: {
            name: "default",
            configuration: {
                separator: options.chunkSeparator ?? "/",
            },
        },
        codecs: options.codecs ?? [],
        fill_value: options.fillValue ?? null,
        dimension_names: options.dimensionNames,
        attributes: options.attributes ?? {},
    };
    await location.store.set(location.resolve("zarr.json").path, jsonEncodeObject(metadata));
    return new Array(location.store, location.path, metadata);
}
