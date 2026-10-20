// Reads that the page started before this script had downloaded, and how the viewer takes them over.
//
// The second inline script of index.html fetches catalog.json and the root zarr.json of the store that the address (or the
// catalog) names, and keeps each pending response in `window.chronozarrEarly`, keyed by its address. The viewer's own read of
// that address is then that response instead of a second request, once. Anything else, and a read that failed there, goes
// to fetch as if nothing had been started.

/** The pending early read of `url` (a promise of a Response, or of null when it failed), once; undefined when there is none. */
export function takeEarlyRead(url, reads = globalThis.chronozarrEarly) {
  const pending = reads?.get(url);
  reads?.delete(url);
  return pending;
}

/**
 * How long the early read of the root metadata took, from its request to its response headers: one round trip to the
 * store, which the viewer paces its coarse stages by. The reader sees none of it when the read was made before the
 * viewer script loaded. Taken once; undefined when the page made no early read of a store, or it failed.
 */
export function takeEarlyRootMs(reads = globalThis.chronozarrEarly) {
  const ms = reads?.rootMs;
  if (reads) delete reads.rootMs;
  return ms;
}

/**
 * A fetch for `catalog.json` and for the reader (`openStore`'s `fetch` option). A plain GET of an address that was read
 * early is answered by that read; a range read, any other method and any other address go to `fetchNow`.
 */
export function fetchWithEarlyReads(fetchNow = (input) => globalThis.fetch(input), reads = globalThis.chronozarrEarly) {
  return (input) => {
    const request = typeof input === 'string' || input instanceof URL ? null : input;
    const plain = !request || (request.method === 'GET' && !request.headers.has('range'));
    const pending = plain ? takeEarlyRead(request ? request.url : String(input), reads) : undefined;
    return pending ? pending.then((response) => response ?? fetchNow(input)) : fetchNow(input);
  };
}
