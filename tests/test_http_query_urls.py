"""Store URLs with a query (signed or token prefixes): object URLs, redaction, end to end."""

from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from pathlib import Path

import pytest
from click.testing import CliRunner

from chronozarr.cli import main
from chronozarr.decode import HttpStore, open_store
from chronozarr.doctor import diagnose
from chronozarr.schema import SchemaError
from chronozarr.store import object_url, redact_url
from chronozarr.view import StoreRequestHandler
from tests.synthetic import build_store, make_truth
from tests.test_doctor import serving

pytestmark = pytest.mark.unit

TOKEN = "s3cr3t-token-value"


class TokenHandler(StoreRequestHandler):
    """Answers 403 unless the request carries `?token=<TOKEN>`."""

    def send_head(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if query.get("token") != [TOKEN]:
            self.send_error(HTTPStatus.FORBIDDEN)
            return None
        return super().send_head()


@pytest.fixture(scope="module")
def token_store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("query_urls") / "store"
    build_store(path, make_truth(3, 2, 40, 50), shard=False, chunk_size=16)
    return path


# --- joining --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "key", "expected"),
    [
        ("https://h/p/store", "zarr.json", "https://h/p/store/zarr.json"),
        ("https://h/p/store/", "zarr.json", "https://h/p/store/zarr.json"),
        ("https://h/p/store//", "0/data/c/0/0/0/0", "https://h/p/store/0/data/c/0/0/0/0"),
        ("https://h/p/store?token=abc", "zarr.json", "https://h/p/store/zarr.json?token=abc"),
        ("https://h/p/store/?token=abc", "zarr.json", "https://h/p/store/zarr.json?token=abc"),
        (
            "https://h/p/store?X-Amz-Signature=a%2Bb&X-Amz-Date=1",
            "0/data/zarr.json",
            "https://h/p/store/0/data/zarr.json?X-Amz-Signature=a%2Bb&X-Amz-Date=1",
        ),
        ("https://h/p/store?token=abc#frag", "zarr.json", "https://h/p/store/zarr.json?token=abc"),
        ("https://h/p/store", "a b/zarr.json", "https://h/p/store/a%20b/zarr.json"),
    ],
)
def test_object_url_inserts_key_in_path_and_copies_query(url, key, expected):
    assert object_url(url, key) == expected


def test_http_store_normalizes_trailing_slash_and_keeps_query():
    assert HttpStore("https://h/p/store/?token=abc").url == "https://h/p/store?token=abc"
    assert HttpStore("https://h/p/store/?token=abc") == HttpStore("https://h/p/store?token=abc")
    assert HttpStore("https://h/p/store/") == HttpStore("https://h/p/store")


# --- redaction ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://h/p/store?token=abc", "https://h/p/store?<redacted>"),
        ("https://h/p/store/zarr.json?a=1&b=2#f", "https://h/p/store/zarr.json?<redacted>"),
        ("https://h/p/store", "https://h/p/store"),
        ("https://h/p/store/", "https://h/p/store/"),
        ("/data/stores/what?.zarr", "/data/stores/what?.zarr"),
    ],
)
def test_redact_url_drops_query_only_from_http_urls(url, expected):
    assert redact_url(url) == expected


def test_http_store_repr_hides_the_query():
    assert TOKEN not in repr(HttpStore(f"https://h/p/store?token={TOKEN}"))


def test_http_store_errors_hide_the_query(token_store):
    with serving(TokenHandler, token_store) as url:
        store = HttpStore(f"{url}?token=wrong")
        with pytest.raises(OSError, match="HTTP 403") as caught:
            store._fetch("zarr.json", "GET")
    assert "wrong" not in str(caught.value)
    assert "?<redacted>" in str(caught.value)


# --- end to end -----------------------------------------------------------------------------


def test_open_store_reads_a_token_url(token_store):
    with serving(TokenHandler, token_store) as url:
        opened = open_store(f"{url}/?token={TOKEN}")
        assert len(opened.times) == 3
        assert opened.read_cell(0, 0, 0, 0).shape[0] == 2


def test_open_store_failure_hides_the_token(token_store):
    with serving(TokenHandler, token_store) as url, pytest.raises(SchemaError) as caught:
        open_store(f"{url}/missing?token={TOKEN}")
    assert TOKEN not in str(caught.value)


def test_doctor_passes_on_a_token_url_and_hides_the_token(token_store):
    with serving(TokenHandler, token_store) as url:
        checks = diagnose(f"{url}?token={TOKEN}")
    failed = [c for c in checks if c.status == "fail"]
    assert not failed, failed
    assert not any(TOKEN in f"{c.name} {c.detail} {c.hint}" for c in checks)


def test_doctor_failure_on_a_bad_token_hides_the_token(token_store):
    with serving(TokenHandler, token_store) as url:
        checks = diagnose(f"{url}?token=nope")
    assert checks[0].status == "fail"
    assert "HTTP 403" in checks[0].detail
    assert "nope" not in checks[0].detail
    assert "?<redacted>" in checks[0].detail


def test_cli_info_and_doctor_do_not_print_the_token(token_store):
    with serving(TokenHandler, token_store) as url:
        target = f"{url}?token={TOKEN}"
        info = CliRunner().invoke(main, ["info", target], catch_exceptions=False)
        doctor = CliRunner().invoke(main, ["doctor", target], catch_exceptions=False)
    assert info.exit_code == 0, info.output
    assert doctor.exit_code == 0, doctor.output
    assert f"{url}?<redacted>" in info.output
    assert f"{url}?<redacted>" in doctor.output
    assert TOKEN not in info.output + doctor.output
