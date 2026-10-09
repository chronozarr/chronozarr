// Accessibility state of the two controls whose values the viewer writes as text: the timeline is a slider, and the
// speed control reads in steps per second. The viewer rewrites #time-label and #speed-label on every change, so the
// attributes follow those nodes and the viewer needs no calls of its own.

const $ = (id) => document.getElementById(id);

/** Run `update` whenever the text of `element` is rewritten. */
function onTextChange(element, update) {
  new MutationObserver(update).observe(element, { childList: true, characterData: true, subtree: true });
}

/** Expose the timeline as an ARIA slider: its value and the keys that move it (Left and Right are the viewer's global shortcuts). */
export function bindTimelineSlider(viewer) {
  const track = $('timeline-track');
  const label = $('time-label');
  onTextChange(label, () => {
    if (!viewer.store) return;
    track.setAttribute('aria-valuemax', String(viewer.store.times.length - 1));
    track.setAttribute('aria-valuenow', String(viewer.t));
    track.setAttribute('aria-valuetext', label.textContent);
  });
  track.addEventListener('keydown', (e) => {
    if (!viewer.store || e.altKey || e.ctrlKey || e.metaKey) return;
    const target = { Home: 0, End: viewer.store.times.length - 1, ArrowUp: viewer.t + 1, ArrowDown: viewer.t - 1 }[e.key];
    if (target === undefined) return;
    e.preventDefault();
    viewer.goToTime(target);
  });
}

/** The speed slider's value is an index into SPEEDS; its text is what the label beside it says, spelled out. */
export function bindSpeedText() {
  const label = $('speed-label');
  const update = () => {
    const text = label.textContent.replace(' /s → ', ' steps per second, delivering ').replace(/ \/s$/, ' steps per second');
    $('speed').setAttribute('aria-valuetext', text);
  };
  onTextChange(label, update);
  update();
}
