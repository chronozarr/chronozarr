// How many round trips a page's scripts need, from a log of its script requests. Used by the browser test of the built
// viewer and by the field-performance scripts (scripts/fieldperf).

/**
 * The longest chain of script requests in which each was started by the one before it: a script that the page itself
 * asks for is level 1, a module that level-1 script imports is level 2, and so on. A browser starts every import of a
 * module as soon as it has read that module, so this is the number of round trips the scripts need however many files
 * there are.
 *
 * @param {Array<{url: string, type: string, initiatorUrl?: string | null}>} requests  one entry per request; only
 *   those of type 'Script' count (the type Chromium's DevTools protocol gives script and module requests). `initiatorUrl`
 *   is the address of the script that caused the request, as `Network.requestWillBeSent` reports it.
 */
export function scriptLevels(requests) {
  const scripts = new Map(requests.filter((request) => request.type === 'Script').map((request) => [request.url, request]));
  const levels = new Map();
  const levelOf = (script, chain = new Set()) => {
    if (levels.has(script.url)) return levels.get(script.url);
    if (chain.has(script.url)) return 1;
    chain.add(script.url);
    const parent = script.initiatorUrl && script.initiatorUrl !== script.url ? scripts.get(script.initiatorUrl) : undefined;
    const level = 1 + (parent ? levelOf(parent, chain) : 0);
    levels.set(script.url, level);
    return level;
  };
  return Math.max(0, ...[...scripts.values()].map((script) => levelOf(script)));
}
