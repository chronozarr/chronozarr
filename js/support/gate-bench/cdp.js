// The network of a page as Chrome's DevTools protocol sees it: a log of the requests to some origins (what they cost on the
// wire and when each started), and the throttling of the link. A runner throttles after the viewer has opened its store: with
// the HTTP cache off, each decode worker fetches its own copy of its script and codec, and a link that slowed those would
// measure the start of the workers, not the chunks the scenario reads.

/** Per-request log of `origins` ({ url, type, startedAt, endedAt, bytes, failed }, times in CDP's monotonic seconds). */
export class NetworkLog {
  #requests = new Map();

  constructor(cdp, origins) {
    cdp.on('Network.requestWillBeSent', (event) => {
      if (!origins.some((origin) => event.request.url.startsWith(origin))) return;
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
