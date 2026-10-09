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

/** `text` with every http(s) URL in it reduced to `scheme://host/path`; a URL that cannot be parsed becomes `[url]`. */
export function redactText(text) {
  return String(text).replace(/https?:\/\/[^\s"'<>)\]]+/gi, (match) => {
    const [, address, punctuation] = /^(.*?)([.,;:!?]*)$/.exec(match);
    return (redactUrl(address) || '[url]') + punctuation;
  });
}
