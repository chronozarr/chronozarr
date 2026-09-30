/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.
 * package: zarrita 0.7.5, license MIT, https://github.com/manzt/zarrita.js
 * file:    dist/src/extension/define-array.js, sha256 of the published file 5b595d474090453078d98a240d92bc6e6d00a3ce0867853423a5c721635ee52c
 * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.
 */
import { assertFactoryResult, createProxy } from "./define.js";
export function defineArrayExtension(factory) {
    return (array, opts) => {
        // @ts-expect-error - factory's opts parameter is wider than `never` at runtime.
        let result = factory(array, opts);
        if (result instanceof Promise) {
            return result.then((overrides) => {
                assertFactoryResult(overrides);
                return createProxy(array, overrides);
            });
        }
        assertFactoryResult(result);
        return createProxy(array, result);
    };
}
