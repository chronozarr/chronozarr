// Store URLs in messages and history. A URL can carry a login (user:password@host) or a signed query
// (?X-Amz-Signature=..., ?sig=..., ?token=...); neither belongs in an error message, a console line or an
// address bar. What is left is the part that names the store: scheme, host and path.

/** `scheme://host/path` of an absolute URL, without userinfo, query or fragment. Anything that is not an absolute URL gives ''. */
export function redactUrl(value) {
  let url;
  try {
    url = new URL(String(value));
  } catch {
    return '';
  }
  return `${url.protocol}//${url.host}${url.pathname}`;
}

/** Replace the exact request URL, then redact other HTTP(S) URLs in the text. */
export function redactText(text, requestUrl) {
  let shown = String(text);
  if (requestUrl) {
    let request;
    try { request = new URL(requestUrl, globalThis.location?.href); } catch { /* no known URL */ }
    if (request && (request.username || request.password || request.search || request.hash)) {
      const safe = redactUrl(request);
      const { pathname, search } = request;
      shown = shown.replaceAll(request.href, safe);
      shown = shown.replaceAll(requestUrl, safe);
      if (search) shown = shown.replaceAll(pathname + search, pathname);
    }
  }
  // Keep bracketed IPv6 addresses intact. An unmatched closing bracket still delimits a URL.
  return shown.replace(/https?:\/\/(?:[^\s"'<>()\[\]]|\[[^\s"'<>()\[\]]*\])+/gi, (match) => {
    const [, address, punctuation] = /^(.*?)([.,;:!?]*)$/.exec(match);
    return (redactUrl(address) || '[url]') + punctuation;
  });
}
