// The network of a page as Chrome's DevTools protocol sees it: a log of the requests to some origins (what they cost on the
// wire and when each started), and the throttling of the link. The page's own session sees the requests of the page; a request
// that a dedicated worker makes itself is neither in the log nor, without Target.setAutoAttach, slowed by the throttling.
// The reader fetches chunks on the main thread, so what a scenario counts and what the link limits are the chunks.

/** Per-request log of `origins` ({ url, type, startedAt, endedAt, bytes, failed }, times in CDP's monotonic seconds). */
export class NetworkLog {
  #requests = new Map();

  constructor(cdp, origins) {
    // An origin matches by its path prefix with the slash: http://127.0.0.1:5000 is not the origin of http://127.0.0.1:50001/.
    const prefixes = origins.map((origin) => (origin.endsWith('/') ? origin : `${origin}/`));
    cdp.on('Network.requestWillBeSent', (event) => {
      if (!prefixes.some((prefix) => event.request.url.startsWith(prefix))) return;
      this.#requests.set(event.requestId, { url: event.request.url, type: event.type, startedAt: event.timestamp, endedAt: null, bytes: 0, failed: false });
    });
    cdp.on('Network.dataReceived', (event) => {
      const request = this.#requests.get(event.requestId);
      if (request) request.bytes += event.encodedDataLength;
    });
    // `encodedDataLength` of the finished request counts its headers too; a request that failed keeps the body bytes it had received.
    cdp.on('Network.loadingFinished', (event) => {
      const request = this.#requests.get(event.requestId);
      if (request) Object.assign(request, { endedAt: event.timestamp, bytes: event.encodedDataLength });
    });
    cdp.on('Network.loadingFailed', (event) => {
      const request = this.#requests.get(event.requestId);
      if (request) Object.assign(request, { endedAt: event.timestamp, failed: true });
    });
  }

  /** Requests started so far, bytes on the wire so far and requests that did not finish (cancelled or failed). */
  snapshot() {
    let bytes = 0;
    let failed = 0;
    for (const request of this.#requests.values()) {
      bytes += request.bytes;
      if (request.failed) failed++;
    }
    return { requests: this.#requests.size, bytes, failed };
  }

  entries() {
    return [...this.#requests.values()];
  }
}

/** A CDP session of the page with its network log on. The HTTP cache is off by default, so that bytes mean bytes on the wire. */
export async function attachNetworkLog(context, page, origins, { cacheDisabled = true } = {}) {
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled });
  return { cdp, log: new NetworkLog(cdp, origins) };
}

/** `link` is { mbps, latencyMs }: the download rate and the delay before each response; null takes the throttling off. */
export async function setLink(cdp, link) {
  const bytesPerSecond = link ? (link.mbps * 1e6) / 8 : -1;
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: link?.latencyMs ?? 0, downloadThroughput: bytesPerSecond, uploadThroughput: bytesPerSecond });
}
