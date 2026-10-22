"""look: open a web page in headless Chromium and say what a person looking at it would see.

    look URL [--wait-ms N] [--timeout-ms N] [--click X,Y | --key KEY]...

Prints the final URL, the title, the visible text, console errors, failed requests, whether the
page's main canvas has painted anything and how long that took, and, for a chronozarr viewer,
the date, product and map position it shows. Saves a screenshot under ~/shots/ and prints its
path. Clicks and keys run in the order given; the report is printed again after each. X,Y are
pixels in the 1280 x 800 window, from its top left corner.
Where a browser would ask whether a page may reach devices on the local network (a link to a
store served by `chronozarr preview`), look answers Allow.
"""

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# playwright is installed in look's own venv by scripts/trial/newcomer/Dockerfile; no extra or
# dependency group of the project carries it, so ty cannot resolve it.
from playwright.sync_api import (  # ty: ignore[unresolved-import]  # newcomer image only
    Error as PlaywrightError,
)
from playwright.sync_api import (  # ty: ignore[unresolved-import]  # newcomer image only
    Page,
    sync_playwright,
)

WINDOW = {"width": 1280, "height": 800}
TEXT_LIMIT = 2500
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--enable-webgl",
]

# A machine can hold public key hashes (base64 SHA-256 of the SPKI, one per line) of CAs whose
# certificates the browser accepts. Chromium on Linux reads its own NSS database rather than
# /etc/ssl/certs, and takes these as --ignore-certificate-errors-spki-list: errors are ignored only
# for a certificate chain that holds one of the keys. The simulated-release harness
# (scripts/trial/lib.sh, site_trust) writes the file; a machine without it trusts the public roots.
TRUSTED_SPKI_FILE = Path("/etc/look/trusted-spki")

# Runs before any page script. Polls the main canvas every 50 ms and records, in milliseconds
# since the navigation began, when it first shows more than its background colour. A canvas is
# read by scaling it down to 48 x 48 pixels: it counts as painted when at least 1% of those
# pixels differ clearly from the most common colour.
MONITOR_JS = """
(() => {
  const probe = document.createElement('canvas');
  probe.width = probe.height = 48;
  const context = probe.getContext('2d', { willReadFrequently: true });
  const mainCanvas = () => {
    const named = document.getElementById('gl-canvas');
    if (named) return named;
    let best = null;
    for (const canvas of document.querySelectorAll('canvas')) {
      if (!best || canvas.width * canvas.height > best.width * best.height) best = canvas;
    }
    return best;
  };
  const sample = () => {
    const canvas = mainCanvas();
    if (!canvas) return { found: false };
    const info = {
      found: true, id: canvas.id || '(no id)', width: canvas.width, height: canvas.height,
      painted: false, fraction: 0,
    };
    if (canvas.width < 2 || canvas.height < 2) return info;
    try {
      context.drawImage(canvas, 0, 0, 48, 48);
      const { data } = context.getImageData(0, 0, 48, 48);
      const counts = new Map();
      for (let i = 0; i < data.length; i += 4) {
        const key = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
        counts.set(key, (counts.get(key) || 0) + 1);
      }
      let common = 0, best = -1;
      for (const [key, n] of counts) if (n > best) { best = n; common = key; }
      let different = 0;
      for (let i = 0; i < data.length; i += 4) {
        const dr = Math.abs(data[i] - (common >> 16));
        const dg = Math.abs(data[i + 1] - ((common >> 8) & 255));
        const db = Math.abs(data[i + 2] - (common & 255));
        if (Math.max(dr, dg, db) > 8) different++;
      }
      info.fraction = different / 2304;
      info.painted = info.fraction >= 0.01;
    } catch (error) {
      info.error = String(error);
    }
    return info;
  };
  window.__look = { firstPaintedMs: null, sample };
  const timer = setInterval(() => {
    if (window.__look.firstPaintedMs !== null) return clearInterval(timer);
    if (sample().painted) window.__look.firstPaintedMs = Math.round(performance.now());
  }, 50);
})();
"""

VIEWER_JS = """
() => {
  const viewer = window.chronozarr && window.chronozarr.viewer;
  const label = document.getElementById('time-label');
  if (!viewer || !viewer.store) {
    return { viewer: Boolean(viewer), label: label ? label.textContent.trim() : null };
  }
  const { store, camera } = viewer;
  const [a, b, c, d, e, f] = store.transform || [1, 0, 0, 0, 1, 0];
  const product = viewer.products[viewer.productIndex];
  const band = product && product.id === 'band' ? viewer.bands[viewer.bandChoice].name : null;
  return {
    viewer: true,
    date: viewer.timeLabel(viewer.t),
    t: viewer.t,
    count: store.times.length,
    product: product ? (product.label || product.name || product.id) : null,
    band,
    zoom: Number((camera.scale / (window.devicePixelRatio || 1)).toPrecision(3)),
    col: Number(camera.cx.toFixed(1)),
    row: Number(camera.cy.toFixed(1)),
    x: Number((a * camera.cx + b * camera.cy + c).toFixed(1)),
    y: Number((d * camera.cx + e * camera.cy + f).toFixed(1)),
    crs: typeof store.crs === 'string' ? store.crs : JSON.stringify(store.crs),
    shareUrl: viewer.shareUrl,
    painted: viewer.paintedT === viewer.t,
  };
}
"""


class Collector:
    """What the page logged since the last report."""

    def __init__(self, page: Page) -> None:
        self.errors: list[str] = []
        self.failed: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.errors.append(f"uncaught: {str(error)[:300]}"))
        page.on(
            "requestfailed",
            lambda request: self.failed.append(f"{request.url[:200]}  {request.failure}"),
        )
        page.on("response", self._response)

    def _console(self, message) -> None:
        if message.type == "error":
            self.errors.append(message.text[:300])

    def _response(self, response) -> None:
        if response.status >= 400:
            self.failed.append(f"{response.url[:200]}  HTTP {response.status}")

    def drain(self) -> tuple[list[str], list[str]]:
        errors, failed = self.errors, self.failed
        self.errors, self.failed = [], []
        return errors, failed


def visible_text(page: Page) -> str:
    raw = page.evaluate("() => document.body ? document.body.innerText : ''")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) > TEXT_LIMIT:
        text = f"{text[:TEXT_LIMIT]}\n... ({len(text) - TEXT_LIMIT} more characters)"
    return text


def describe_canvas(info: dict, first_painted_ms: int | None) -> str:
    if not info.get("found"):
        return "none on the page"
    size = f"{info['id']} {info['width']}x{info['height']}"
    if info.get("error"):
        return f"{size}, could not be read: {info['error']}"
    if not info["painted"]:
        return f"{size}, blank (nothing but its background colour)"
    share = f"{info['fraction'] * 100:.0f}% of sampled pixels differ from the background"
    when = (
        f", first painted {first_painted_ms} ms after the navigation began"
        if first_painted_ms is not None
        else ""
    )
    return f"{size}, painted ({share}){when}"


def describe_viewer(state: dict) -> list[str]:
    if not state.get("viewer"):
        return ["viewer:     no chronozarr viewer on this page"]
    if "date" not in state:
        return [f"viewer:     no store open yet; date control shows {state.get('label')!r}"]
    shows = f"{state['product']}" + (f" ({state['band']})" if state["band"] else "")
    return [
        f"viewer:     date {state['date']} (timestep {state['t'] + 1} of {state['count']}), "
        f"showing {shows}, "
        f"frame {'complete' if state['painted'] else 'not complete'}",
        f"position:   zoom {state['zoom']} screen px per data px, "
        f"centre column {state['col']} row {state['row']}, "
        f"x {state['x']} y {state['y']} in {state['crs']}",
        f"view link:  {state['shareUrl']}",
    ]


def report(
    page: Page,
    collector: Collector,
    label: str,
    shot_dir: Path,
    shot_number: int,
    loaded_at: dict | None,
) -> None:
    canvas = page.evaluate("() => window.__look ? window.__look.sample() : { found: false }")
    first = page.evaluate("() => window.__look ? window.__look.firstPaintedMs : null")
    viewer = page.evaluate(VIEWER_JS)
    errors, failed = collector.drain()
    shot = shot_dir / f"look-{datetime.now():%Y%m%d-%H%M%S}-{shot_number:02d}.png"
    page.screenshot(path=str(shot))

    print(f"== {label} ==")
    print(f"url:        {page.url}")
    print(f"title:      {page.title()!r}")
    if loaded_at is not None:
        print(
            f"loaded:     {loaded_at['dom']} ms to DOM ready, {loaded_at['load']} ms to load "
            "(from the navigation start)"
        )
    print(f"canvas:     {describe_canvas(canvas, first)}")
    for line in describe_viewer(viewer):
        print(line)
    print(f"console errors ({len(errors)}):")
    for line in errors:
        print(f"  {line}")
    print(f"failed requests ({len(failed)}):")
    for line in failed:
        print(f"  {line}")
    print(f"screenshot: {shot}")
    print("--- visible text ---")
    print(visible_text(page))
    print()


def wait_for_paint(page: Page, timeout_ms: int) -> None:
    """Return once the main canvas has painted, the page has no canvas, or the time is up."""
    started = time.monotonic()
    loaded_at = None
    while (time.monotonic() - started) * 1000 < timeout_ms:
        if page.evaluate("() => window.__look && window.__look.firstPaintedMs !== null"):
            return
        has_canvas = page.evaluate("() => document.querySelector('canvas') !== null")
        ready = page.evaluate("() => document.readyState === 'complete'")
        if ready and loaded_at is None:
            loaded_at = time.monotonic()
        if ready and loaded_at is not None and not has_canvas and time.monotonic() - loaded_at > 2:
            return
        page.wait_for_timeout(100)


def navigation_timing(page: Page) -> dict:
    return page.evaluate(
        "() => { const n = performance.getEntriesByType('navigation')[0];"
        " return n ? { dom: Math.round(n.domContentLoadedEventEnd),"
        " load: Math.round(n.loadEventEnd) } : { dom: -1, load: -1 }; }"
    )


class Step(argparse.Action):
    """Collect --click and --key in the order they were typed."""

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        if option_string is None:
            raise ValueError(
                f"{self.dest}: Step collects options (--click, --key), not positionals"
            )
        steps = getattr(namespace, "steps", None) or []
        steps.append((option_string.lstrip("-"), values))
        namespace.steps = steps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="look", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url")
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=1500,
        help="settle time after the first paint and after each click or key (default 1500)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30000,
        help="longest wait for the page to load and its canvas to paint (default 30000)",
    )
    parser.add_argument(
        "--click", action=Step, metavar="X,Y", help="click at X,Y in the 1280 x 800 window"
    )
    parser.add_argument(
        "--key",
        action=Step,
        metavar="KEY",
        help="press a key, for example Space, ArrowRight, Shift+ArrowLeft",
    )
    args = parser.parse_args()
    args.steps = getattr(args, "steps", None) or []
    for kind, value in args.steps:
        if kind == "click" and not re.fullmatch(r"\d+,\d+", value):
            parser.error(f"--click wants X,Y in pixels, got {value!r}")
    return args


def chromium_args() -> list[str]:
    args = list(CHROMIUM_ARGS)
    if TRUSTED_SPKI_FILE.is_file():
        lines = TRUSTED_SPKI_FILE.read_text().splitlines()
        hashes = [line.strip() for line in lines if line.strip()]
        if hashes:
            args.append(f"--ignore-certificate-errors-spki-list={','.join(hashes)}")
    return args


def main() -> int:
    args = parse_args()
    shot_dir = Path.home() / "shots"
    shot_dir.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=chromium_args())
        context = browser.new_context(viewport=WINDOW, locale="en-US", timezone_id="UTC")
        # Chrome asks before a page on the internet reads 127.0.0.1; nobody is here to click Allow.
        context.grant_permissions(["local-network-access"])
        context.add_init_script(MONITOR_JS)
        page = context.new_page()
        collector = Collector(page)
        try:
            page.goto(args.url, wait_until="load", timeout=args.timeout_ms)
        except PlaywrightError as error:
            errors, failed = collector.drain()
            print(f"could not open {args.url}")
            print(f"  {str(error).splitlines()[0]}")
            for line in errors + failed:
                print(f"  {line}")
            browser.close()
            return 2
        wait_for_paint(page, args.timeout_ms)
        page.wait_for_timeout(args.wait_ms)
        number = 1
        report(page, collector, "load", shot_dir, number, navigation_timing(page))
        for kind, value in args.steps:
            number += 1
            if kind == "click":
                x, y = (int(part) for part in value.split(","))
                page.mouse.click(x, y)
                label = f"step {number - 1}, after click {x},{y}"
            else:
                page.keyboard.press(value)
                label = f"step {number - 1}, after key {value}"
            page.wait_for_timeout(args.wait_ms)
            report(page, collector, label, shot_dir, number, None)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
