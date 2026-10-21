"""A fake slow link for counting the HTTP round trips that code makes one after another."""

from __future__ import annotations

import threading
import time
import urllib.request
from urllib.parse import urlsplit

import pytest


class RoundTrips:
    """Record every `urllib.request.urlopen` call and hold each one for `delay` seconds.

    The delay stands in for a slow link such as a quick tunnel. `rounds()` is the longest chain
    of requests in which each started after the previous one ended: the round trips the code
    waited for one after another. Requests made together overlap and count once.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, delay: float = 0.2) -> None:
        self.requests: list[tuple[float, float, str]] = []
        lock = threading.Lock()
        real = urllib.request.urlopen

        def urlopen(request, *args, **kwargs):
            started = time.perf_counter()
            time.sleep(delay)
            try:
                return real(request, *args, **kwargs)
            finally:
                with lock:
                    path = urlsplit(request.full_url).path
                    self.requests.append((started, time.perf_counter(), path))

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def paths(self) -> list[str]:
        return [path for _, _, path in self.requests]

    def rounds(self) -> int:
        count, last_end = 0, float("-inf")
        for started, ended, _ in sorted(self.requests, key=lambda request: request[1]):
            if started >= last_end:
                count, last_end = count + 1, ended
        return count
