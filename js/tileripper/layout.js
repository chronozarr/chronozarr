// Layout breakpoints, in CSS pixels of viewport width.
//
// index.html repeats these numbers in its media queries (CSS cannot import them) and viewer-layout.test.js checks that
// the two agree, so a change here that misses the stylesheet fails a test.

/** Below this width the inspector is a drawer over the map (opened by a click, closed by Escape); from it on, a side panel. */
export const DRAWER_BELOW = 900;

/** Below this width the product buttons give way to one select (CSS only), so the header keeps room for the catalog and the stretch controls. */
export const COMPACT_BELOW = 700;

/** 'drawer' or 'panel': how the inspector is shown at this viewport width. */
export function inspectorLayout(width) {
  return width < DRAWER_BELOW ? 'drawer' : 'panel';
}
