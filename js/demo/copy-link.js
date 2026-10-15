// The small, non-modal "Copy link" control in the full viewer.

export const COPY_FEEDBACK_MS = 2500;

/** Write `url` with the browser clipboard API, or fail clearly when it is unavailable. */
export async function copyLink(url, { clipboard = globalThis.navigator?.clipboard } = {}) {
  if (!clipboard?.writeText) throw new Error('Clipboard access is unavailable');
  await clipboard.writeText(url);
}

const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);

/** Whether the store in a viewer link is on the computer that copied it, so the link opens nowhere else. */
export function linksToThisComputer(url) {
  try {
    const link = new URL(url);
    const store = new URL(link.searchParams.get('store') ?? '.', link);
    return LOOPBACK.has(store.hostname) || store.hostname.endsWith('.localhost');
  } catch {
    return false;
  }
}

/**
 * Bind the copy control. `getUrl` owns the viewer's permalink and redaction policy; this module only copies its
 * result and makes the outcome visible both on the button and to assistive technology.
 */
export function bindCopyLink({ button, status, getUrl, clipboard, feedbackMs = COPY_FEEDBACK_MS, timer = globalThis }) {
  const label = button.textContent;
  let restoreTimer = null;

  const show = (nextLabel, message) => {
    if (restoreTimer !== null) timer.clearTimeout(restoreTimer);
    button.textContent = nextLabel;
    status.textContent = message;
    restoreTimer = timer.setTimeout(() => {
      button.textContent = label;
      status.textContent = '';
      restoreTimer = null;
    }, feedbackMs);
  };

  const copy = async () => {
    const url = getUrl();
    if (!url) return false;
    try {
      await copyLink(url, { clipboard });
      if (linksToThisComputer(url)) {
        show('Copied (local only)', 'Copied a link that opens only on this computer. For a link anyone can open, run chronozarr share STORE in a terminal.');
      } else {
        show('Link copied', 'Link copied.');
      }
      return true;
    } catch {
      show('Copy address bar', 'Could not copy the link. Copy the address bar.');
      return false;
    }
  };

  button.addEventListener('click', () => void copy());
  return { copy };
}
