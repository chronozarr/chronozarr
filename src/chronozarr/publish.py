"""`chronozarr publish`: upload a local store to static hosting and verify it from the outside.

The module separates what every host needs (validate, plan, order, resume, verify, link) from what
one host provides (`StorageAdapter`). `chronozarr._publish_s3` implements the adapter for AWS S3
and, through an endpoint URL, Cloudflare R2 and other S3-compatible services;
`chronozarr._publish_gcs` for Google Cloud Storage and `chronozarr._publish_azure` for Azure Blob
Storage. `chronozarr._publish_adapters.open_adapter` picks one by destination scheme. Another
provider needs a new `_publish_<name>.py` and a scheme there.

Two addresses are kept apart throughout. The destination (`s3://bucket/prefix`) is where the
credentials write. The public URL (`https://...`) is where a browser reads. Nothing here derives
one from the other except through the adapter, and the viewer link is built from the public URL
after `chronozarr.doctor` has passed against it.

Upload order follows docs/hosting.md section 2: chunk and shard objects, then the group and array
`zarr.json` files, then the root `zarr.json` (which carries the consolidated metadata). A phase
starts only after every object of the previous phase is stored, so a reader that finds the root
finds a complete store.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from chronozarr import schema
from chronozarr.doctor import DEFAULT_ORIGIN, Check, diagnose
from chronozarr.view import viewer_url

IMMUTABLE = "public, max-age=31536000, immutable"
SHORT_TTL = "public, max-age=300"
PHASES = ("chunks", "group and array metadata", "root metadata")
_MD5_HEX = re.compile(r"^[0-9a-f]{32}$")
_MUTABLE_KEY = re.compile(r"^(\d+/time/c/0|volatility/c/.*)$")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")

Log = Callable[[str], None]


class PublishError(Exception):
    """A publish step failed in a way the user can act on. The message says how."""


# ---------------------------------------------------------------------------------------------
# Destination and public URL
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Where credentials write: `scheme://bucket/prefix`. Not a browser URL."""

    scheme: str
    bucket: str
    prefix: str  # no leading or trailing slash; empty means the bucket root

    @property
    def uri(self) -> str:
        return f"{self.scheme}://{self.bucket}" + (f"/{self.prefix}" if self.prefix else "")

    def key(self, relative: str) -> str:
        return f"{self.prefix}/{relative}" if self.prefix else relative


def _redacted(url: str) -> str:
    """`url` without credentials, query string or fragment, for messages and logs."""
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc:
        return "<not a URL>" if "@" in url or "?" in url else url
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"


def parse_destination(text: str) -> Destination:
    parts = urllib.parse.urlsplit(text)
    if parts.scheme in ("http", "https"):
        raise PublishError(
            f"--destination {_redacted(text)!r} is a web URL. The destination is the storage "
            "location, such as s3://BUCKET/PREFIX. Pass the address browsers read from as "
            "--public-url."
        )
    if not parts.scheme or not parts.netloc:
        raise PublishError(
            f"--destination {_redacted(text)!r} is not a storage location. Expected "
            "SCHEME://BUCKET/PREFIX, for example s3://my-bucket/aoi/store-v1."
        )
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise PublishError(
            "--destination takes only SCHEME://BUCKET/PREFIX: no credentials, query or fragment. "
            "Credentials come from the provider's own credential chain."
        )
    prefix = parts.path.strip("/")
    segments = prefix.split("/") if prefix else []
    if any(s in ("", ".", "..") for s in segments):
        raise PublishError(f"--destination prefix {prefix!r} has an empty, '.' or '..' segment.")
    return Destination(parts.scheme, parts.netloc, prefix)


def check_public_url(url: str) -> str:
    """Return the normalised public store URL or raise. It ends up in a shareable link."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PublishError(
            f"--public-url {_redacted(url)!r} is not an https URL. Give the address browsers read "
            "the store from, such as https://data.example.com/aoi/store-v1 (not the s3:// "
            "destination)."
        )
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise PublishError(
            f"--public-url {_redacted(url)!r} is plain http. The viewer is served over https and "
            "browsers block mixed content; use an https URL."
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PublishError(
            "--public-url must not carry credentials, a query string (signed URLs) or a fragment; "
            "it ends up in a link that other people open."
        )
    return url.rstrip("/")


# ---------------------------------------------------------------------------------------------
# Adapter boundary
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteObject:
    """An object already in the destination. `etag` is the hex MD5 of the content when the
    provider reports one for this object, else ''. Size alone then decides whether it matches."""

    size: int
    etag: str


class StorageAdapter(Protocol):
    """What one storage provider supplies. Keys are full object keys (prefix included).

    Methods raise `PublishError` with an actionable message for expected failures (missing
    permission, bucket not found). CORS rules are in the provider's own JSON shape: `read_cors`,
    `write_cors` and `cors_rule` agree with each other and `publish` never looks inside them.
    """

    label: str  # human name for messages, for example "AWS S3" or "Cloudflare R2"

    def endpoint_description(self) -> str:
        """The authenticated endpoint in words. Never includes credentials."""
        ...

    def default_public_url(self, prefix: str) -> str | None:
        """The provider's own browser-readable URL for `prefix`, or None when it has none.

        None means the user must pass --public-url (a custom domain or CDN).
        """
        ...

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        """Every object under `prefix` plus '/' (the whole bucket when `prefix` is '')."""
        ...

    def put_object(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        """Store the file at `path` as `key`, replacing an object with that key."""
        ...

    def read_cors(self) -> list[dict[str, Any]]:
        """The bucket's current CORS rules; [] when none are configured."""
        ...

    def write_cors(self, rules: list[dict[str, Any]]) -> None:
        """Replace the bucket's whole CORS configuration with `rules`."""
        ...

    def cors_rule(self) -> dict[str, Any]:
        """The one rule that lets the viewer read a store (GET, HEAD, Range, Content-Range)."""
        ...

    def access_help(self) -> str:
        """What to do when the objects are stored but browsers cannot read them."""
        ...


# ---------------------------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalObject:
    key: str  # relative to the store root, '/'-separated
    path: Path
    size: int
    phase: int  # index into PHASES

    @property
    def cache_control(self) -> str:
        """Objects an append rewrites in place get a short lifetime; the rest never change."""
        if self.key.rsplit("/", 1)[-1] == "zarr.json" or _MUTABLE_KEY.match(self.key):
            return SHORT_TTL
        return IMMUTABLE

    @property
    def content_type(self) -> str:
        return "application/json" if self.key.endswith("zarr.json") else "application/octet-stream"


@dataclass(frozen=True)
class PublishPlan:
    store: Path
    destination: Destination
    public_url: str
    objects: tuple[LocalObject, ...]  # sorted by (phase, key)
    public_url_derived: bool = False  # the adapter supplied it; the user did not
    skipped_hidden: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(o.size for o in self.objects)

    def summary(self, adapter: StorageAdapter, state: str = "") -> str:
        immutable = sum(o.cache_control == IMMUTABLE for o in self.objects)
        lines = [
            f"store        {self.store} (conforms to chronozarr {schema.SPEC_VERSION})",
            f"destination  {self.destination.uri} via {adapter.endpoint_description()}",
            f"public URL   {self.public_url} "
            + (
                f"(derived from the {adapter.label} endpoint; pass --public-url for a CDN or "
                "custom domain; checked after the upload)"
                if self.public_url_derived
                else "(checked after the upload)"
            ),
            f"objects      {_count(len(self.objects), 'object')}, {self.total_bytes:,} bytes",
        ]
        for index, name in enumerate(PHASES):
            in_phase = [o for o in self.objects if o.phase == index]
            lines.append(
                f"  {index + 1}. {name}: {_count(len(in_phase), 'object')}, "
                f"{sum(o.size for o in in_phase):,} bytes"
            )
        lines.append(
            f"cache        {_count(immutable, 'object')} `{IMMUTABLE}`, "
            f"{_count(len(self.objects) - immutable, 'object')} `{SHORT_TTL}`"
        )
        if state:
            lines.append(f"prefix       {state}")
        if self.skipped_hidden:
            lines.append(
                f"skipped      {self.skipped_hidden} hidden file(s) (names start with '.')"
            )
        return "\n".join(lines)


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _phase(key: str) -> int:
    if key == "zarr.json":
        return 2
    return 1 if key.endswith("/zarr.json") else 0


def plan_store(
    store: Path, destination: Destination, adapter: StorageAdapter, public_url: str | None
) -> PublishPlan:
    """Validate `store`, settle the public URL and list what would be uploaded.

    Reads the local disk first; the adapter is asked for a default public URL only when
    `public_url` is None, and only after the store validated.
    """
    if not (store / "zarr.json").is_file():
        raise PublishError(
            f"{store} is not a chronozarr store: no zarr.json in it. Pass the store directory "
            "that `chronozarr encode` wrote."
        )
    try:
        problems = schema.validate(str(store))
    except Exception as exc:  # unreadable metadata raises zarr, JSON or schema errors alike
        raise PublishError(
            f"{store} could not be read as a chronozarr store ({type(exc).__name__}: {exc}); "
            "nothing was uploaded. Run `chronozarr validate` on it, or re-encode."
        ) from exc
    if problems:
        shown = "\n".join(f"  {p}" for p in problems[:10])
        more = f"\n  ... {len(problems) - 10} more" if len(problems) > 10 else ""
        raise PublishError(
            f"{store} does not conform to chronozarr {schema.SPEC_VERSION}; nothing was "
            f"uploaded.\n{shown}{more}\nRun `chronozarr validate {store}` for the full list."
        )
    derived = public_url is None
    if public_url is None:
        public_url = adapter.default_public_url(destination.prefix)
        if public_url is None:
            raise PublishError(
                f"--public-url is required for {adapter.label}: its storage endpoint is "
                "authenticated and is not a browser address. Pass the https address of a custom "
                f"domain or CDN that serves {destination.uri}, for example "
                f"https://data.example.com/{destination.prefix}."
            )
    public_url = check_public_url(public_url)
    objects: list[LocalObject] = []
    hidden = 0
    for path in store.rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(store).as_posix()
        if any(part.startswith(".") for part in key.split("/")):
            hidden += 1
            continue
        objects.append(LocalObject(key, path, path.stat().st_size, _phase(key)))
    objects.sort(key=lambda o: (o.phase, o.key))
    return PublishPlan(store, destination, public_url, tuple(objects), derived, hidden)


# ---------------------------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------------------------


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_content(local: LocalObject, remote: RemoteObject) -> bool:
    if local.size != remote.size:
        return False
    return not _MD5_HEX.match(remote.etag) or _md5(local.path) == remote.etag


@dataclass(frozen=True)
class DestinationState:
    """What the destination prefix holds, compared with the plan."""

    identical: frozenset[str]  # full keys stored with the same size and checksum
    different: frozenset[str]  # full keys of plan objects stored with other content
    foreign: frozenset[str]  # full keys that are not in the plan at all

    @property
    def conflicts(self) -> bool:
        return bool(self.different or self.foreign)

    def describe(self, overwrite: bool) -> str:
        if not (self.identical or self.conflicts):
            return "empty: a fresh prefix, everything will be uploaded"
        counts = (
            f"{len(self.identical):,} identical objects stored, "
            f"{len(self.different):,} differing, {len(self.foreign):,} not in the store"
        )
        if not self.conflicts:
            return f"{counts}: an earlier upload, which will resume"
        if overwrite:
            return f"{counts}: --overwrite replaces the differing objects and deletes nothing"
        return f"{counts}: publishing would be refused without --overwrite"


def inspect_destination(plan: PublishPlan, adapter: StorageAdapter) -> DestinationState:
    """List the destination prefix and compare it with the plan. Writes nothing."""
    dest = plan.destination
    remote = adapter.list_objects(dest.prefix)
    local = {dest.key(o.key): o for o in plan.objects}
    shared = {key for key in remote if key in local}
    different = {key for key in shared if not _same_content(local[key], remote[key])}
    return DestinationState(
        identical=frozenset(shared - different),
        different=frozenset(different),
        foreign=frozenset(set(remote) - set(local)),
    )


@dataclass(frozen=True)
class UploadReport:
    uploaded: int
    skipped: int
    uploaded_bytes: int


def upload(
    plan: PublishPlan,
    adapter: StorageAdapter,
    *,
    overwrite: bool = False,
    workers: int = 16,
    log: Log = lambda _: None,
) -> UploadReport:
    """Store every object of `plan`, chunks first and the root `zarr.json` last.

    Objects already in the destination with the same size and checksum are skipped, so running
    the same command again resumes an interrupted upload and does nothing once it is complete.
    A prefix holding any other object is refused unless `overwrite` is set; `overwrite`
    replaces objects with different content and never deletes anything.
    """
    dest = plan.destination
    state = inspect_destination(plan, adapter)
    if state.conflicts and not overwrite:
        examples = ", ".join(sorted(state.foreign | state.different)[:3])
        raise PublishError(
            f"{dest.uri} already holds {len(state.foreign)} object(s) that are not in this store "
            f"and {len(state.different)} that differ from it (for example {examples}). Refusing "
            "to overwrite. Publish to a fresh prefix (a new name or version, since objects are "
            "cached for a year), or pass --overwrite to replace differing objects. Objects not "
            "in the store are never deleted."
        )
    todo = [o for o in plan.objects if dest.key(o.key) not in state.identical]
    skipped = len(plan.objects) - len(todo)
    if state.identical:
        log(f"resuming: {skipped:,} of {len(plan.objects):,} objects already stored and identical")
    done = 0
    for index, name in enumerate(PHASES):
        batch = [o for o in todo if o.phase == index]
        if not batch:
            continue
        errors: list[tuple[str, Exception]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    adapter.put_object,
                    dest.key(o.key),
                    o.path,
                    cache_control=o.cache_control,
                    content_type=o.content_type,
                ): o
                for o in batch
            }
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                try:
                    future.result()
                except Exception as exc:  # providers raise their own types; all reported below
                    errors.append((futures[future].key, exc))
                    for pending in futures:
                        pending.cancel()
                else:
                    done += 1
                    if done % 500 == 0 or done == len(todo):
                        log(f"uploaded {done:,} of {len(todo):,} objects")
        if errors:
            key, exc = errors[0]
            raise PublishError(
                f"upload stopped in phase {index + 1} ({name}): {len(errors)} object(s) failed, "
                f"first {key}: {type(exc).__name__}: {exc}. Later phases were not started, so the "
                "store is not complete and has no root zarr.json yet. Run the same command again "
                "to resume; stored objects are skipped."
            ) from exc
    return UploadReport(done, skipped, sum(o.size for o in todo))


# ---------------------------------------------------------------------------------------------
# Verify and link
# ---------------------------------------------------------------------------------------------


def _is_cors_failure(check: Check) -> bool:
    return check.status == "fail" and check.name.startswith("CORS")


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == "fail"]


def format_checks(checks: list[Check]) -> str:
    labels = {"ok": "[ ok ]", "info": "[info]", "warn": "[warn]", "fail": "[FAIL]"}
    width = max(len(c.name) for c in checks)
    lines: list[str] = []
    for check in checks:
        lines.append(f"{labels[check.status]} {check.name.ljust(width)}  {check.detail}")
        if check.hint and check.status in ("warn", "fail"):
            lines.append(f"       fix: {check.hint}")
    counts = {s: sum(c.status == s for c in checks) for s in labels}
    lines.append(
        f"{counts['ok']} ok, {counts['info']} info, {counts['warn']} warning(s), "
        f"{counts['fail']} failure(s)"
    )
    return "\n".join(lines)


def merged_cors(existing: list[dict[str, Any]], required: dict[str, Any]) -> list[dict[str, Any]]:
    """Existing rules unchanged and in order, then `required` unless one is already there.

    Appending last keeps every request that an existing rule already answers answered the same
    way: S3-style CORS uses the first rule that matches.
    """
    return existing if required in existing else [*existing, required]


@dataclass(frozen=True)
class PublishResult:
    report: UploadReport
    checks: list[Check]
    link: str | None  # None unless the hosted store passed every failing-level check
    cors_changed: bool = False
    notes: tuple[str, ...] = ()  # what happened to CORS, for the caller to print after the checks


def publish(
    plan: PublishPlan,
    adapter: StorageAdapter,
    *,
    overwrite: bool = False,
    apply_cors: bool = False,
    workers: int = 16,
    log: Log = lambda _: None,
    check_store: Callable[[str], list[Check]] = lambda url: diagnose(url, origin=DEFAULT_ORIGIN),
) -> PublishResult:
    """Upload, run the doctor checks against the public URL, and return a link only if they pass.

    The bucket's CORS configuration changes only when `apply_cors` is set and a CORS check
    failed; the existing rules are kept. Public access settings are never changed.
    """
    report = upload(plan, adapter, overwrite=overwrite, workers=workers, log=log)
    log(
        f"stored {report.uploaded:,} objects ({report.uploaded_bytes:,} bytes), "
        f"{report.skipped:,} already present"
    )
    log(f"checking {plan.public_url} the way a browser reads it")
    checks = check_store(plan.public_url)
    cors_changed = False
    notes: tuple[str, ...] = ()
    if any(_is_cors_failure(c) for c in checks):
        cors_changed, note = _handle_cors(adapter, apply_cors)
        notes = (note,)
        if cors_changed:
            checks = check_store(plan.public_url)
    link = None if failures(checks) else viewer_url(plan.public_url)
    return PublishResult(report, checks, link, cors_changed, notes)


def _handle_cors(adapter: StorageAdapter, apply_cors: bool) -> tuple[bool, str]:
    """Read the bucket's CORS rules; with `apply_cors`, add the viewer rule.

    Returns whether the configuration changed and a sentence saying what happened.
    """
    try:
        existing = adapter.read_cors()
    except PublishError as exc:
        return False, f"could not read the bucket's CORS configuration: {exc}"
    required = adapter.cors_rule()
    if required in existing:
        return False, (
            "the bucket's CORS configuration already has the viewer rule, so the failure comes "
            "from elsewhere (a CDN in front of the bucket, or an earlier rule that matches first)."
        )
    if not apply_cors:
        return False, (
            f"the bucket has {len(existing)} CORS rule(s). Pass --apply-cors to keep them and add "
            f"this rule, or add it yourself:\n{json.dumps(required, indent=2)}"
        )
    adapter.write_cors(merged_cors(existing, required))
    return True, f"added the viewer CORS rule after the bucket's {len(existing)} existing rule(s)"
