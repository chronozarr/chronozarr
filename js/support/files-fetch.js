// A fetch() over an in-memory file map (like the one buildSyntheticStore builds), with byte ranges,
// HEAD, and scripted failures, for testing the HTTP layer.

/**
 * @param {Map<string, Uint8Array>} files keys like '/zarr.json'
 * @param {{ basePath?: string, fail?: (info: {key:string, method:string, range:string|null, attempt:number}) => null|'network'|number, delayMs?: number, log?: object[] }} [options]
 *   `fail` returns null to serve normally, 'network' to reject with TypeError, or an HTTP status to answer with.
 */
export function filesFetch(files, { basePath = '/store', fail = () => null, delayMs = 0, log = [] } = {}) {
  const attempts = new Map();
  let inFlight = 0;
  const stats = { peak: 0 };
  const fetchImpl = async (request) => {
    const url = new URL(request.url);
    const key = url.pathname.slice(basePath.length);
    const range = request.headers.get('Range');
    const attemptKey = `${request.method} ${key} ${range}`;
    const attempt = (attempts.get(attemptKey) ?? 0) + 1;
    attempts.set(attemptKey, attempt);
    log.push({ key, method: request.method, range, attempt });
    inFlight++;
    stats.peak = Math.max(stats.peak, inFlight);
    try {
      if (delayMs > 0) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, delayMs);
          request.signal.addEventListener('abort', () => {
            clearTimeout(timer);
            reject(new DOMException('Aborted', 'AbortError'));
          });
        });
      }
      request.signal.throwIfAborted();
      const failure = fail({ key, method: request.method, range, attempt });
      if (failure === 'network') throw new TypeError('Failed to fetch');
      if (typeof failure === 'number') return new Response(null, { status: failure, statusText: 'scripted' });
      const file = files.get(key);
      if (!file) return new Response(null, { status: 404 });
      if (request.method === 'HEAD') return new Response(null, { status: 200, headers: { 'Content-Length': String(file.length) } });
      const match = /^bytes=(\d*)-(\d*)$/.exec(range ?? '');
      if (!match) return new Response(file, { status: 200, headers: { 'Content-Length': String(file.length) } });
      const start = match[1] === '' ? Math.max(0, file.length - Number(match[2])) : Number(match[1]);
      const end = match[1] !== '' && match[2] !== '' ? Math.min(file.length - 1, Number(match[2])) : file.length - 1;
      const body = file.slice(start, end + 1);
      return new Response(body, { status: 206, headers: { 'Content-Length': String(body.length) } });
    } finally {
      inFlight--;
    }
  };
  fetchImpl.stats = stats;
  return fetchImpl;
}
