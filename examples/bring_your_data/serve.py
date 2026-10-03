"""Preview a static bundle on localhost with byte ranges; not a production server."""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path

from chronozarr.view import StoreServer

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    if not (bundle / "bundle.json").is_file():
        parser.error("Not a bundle: bundle.json is missing")
    server = StoreServer(bundle, port=args.port)
    print(f"Viewer: {server.url}/index.html", flush=True)
    print(f"Embed:  {server.url}/examples/embed.html", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        server.close()
