"""`chronozarr publish --update`: put appended timesteps on a store that is already published.

The prefix keeps its public URL, so every link already shared stays valid. Only the objects an
append produces are uploaded. Nothing is deleted and no existing chunk is replaced.

What makes this safe is a property of the layout, not of the upload. An unsharded append writes
new chunk keys for the new timesteps and rewrites only `zarr.json` files, each level's
`time/c/0` and the `volatility` chunks. Every chunk that an already-published root refers to
keeps its key and its bytes, so a reader holding the old root, or a CDN serving it, still finds
everything it asks for. The upload order (`chronozarr.publish.PHASES`) puts the objects that
change in place after the objects that are new, and the root `zarr.json` last.

This is not a transaction. Readers can observe the store between two puts; docs/append.md says
what each phase exposes and how to resume. A sharded append rewrites the trailing shard, a chunk
object that readers and CDNs may hold for a year; `--update` refuses it instead of replacing it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable

from chronozarr import schema
from chronozarr.doctor import DEFAULT_ORIGIN, USER_AGENT, Check, diagnose
from chronozarr.publish import (
    SHORT_TTL,
    LocalObject,
    Log,
    PublishError,
    PublishPlan,
    PublishResult,
    StorageAdapter,
    UploadReport,
    put_objects,
    same_content,
    verify_hosted,
)

SHORT_TTL_SECONDS = 300
_LISTED = 3  # keys named in a message before "and N more"


@runtime_checkable
class ReadableStorage(Protocol):
    """An adapter that can read one object back. `--update` needs it to read the hosted root."""

    def read_object(self, key: str) -> bytes | None:
        """The object's bytes, or None when the key does not exist."""
        ...


@dataclass(frozen=True)
class UpdatePlan:
    """What publishing the local store over the hosted one would do."""

    plan: PublishPlan  # the whole local store
    new: tuple[LocalObject, ...]  # not in the destination
    changed: tuple[LocalObject, ...]  # in the destination with other content; allowed to change
    unchanged: int  # in the destination with the same size and checksum
    hosted_times: int
    local_times: int

    @property
    def todo(self) -> tuple[LocalObject, ...]:
        return tuple(sorted((*self.new, *self.changed), key=lambda o: (o.phase, o.key)))

    @property
    def upload_plan(self) -> PublishPlan:
        return replace(self.plan, objects=self.todo)

    def summary(self, adapter: StorageAdapter) -> str:
        added = self.local_times - self.hosted_times
        if self.todo:
            state = (
                f"{self.hosted_times:,} timesteps hosted, {self.local_times:,} local "
                f"({added:,} to add): {len(self.new):,} new object(s), {len(self.changed):,} "
                f"changed in place, {self.unchanged:,} unchanged and skipped"
            )
        else:
            state = f"already up to date: {self.hosted_times:,} timesteps, nothing to upload"
        return self.upload_plan.summary(adapter, state)


def _read_json(raw: bytes, where: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise PublishError(f"{where} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PublishError(f"{where} is not a Zarr group document")
    return document


def _chronozarr_block(document: dict[str, Any], where: str) -> schema.Chronozarr:
    try:
        return schema.parse_chronozarr(document["attributes"]["chronozarr"])
    except (KeyError, TypeError, schema.SchemaError) as exc:
        raise PublishError(
            f"{where} is not a chronozarr {schema.SPEC_VERSION} root group "
            f"({type(exc).__name__}: {exc}). --update appends to a published store of this "
            "version; publish to an empty prefix instead."
        ) from exc


def _prefix_problems(hosted: schema.Chronozarr, local: schema.Chronozarr) -> list[str]:
    """Every way the hosted store is not an earlier state of the local store. Empty if it is."""
    problems: list[str] = []
    n_hosted, n_local = len(hosted.times), len(local.times)
    if n_hosted > n_local:
        problems.append(
            f"the hosted store has {n_hosted} timesteps and the local store {n_local}: the local "
            "copy is behind the hosted one"
        )
    elif hosted.times != local.times[:n_hosted]:
        pairs = zip(hosted.times, local.times, strict=False)
        first = next(i for i, (a, b) in enumerate(pairs) if a != b)
        problems.append(
            f"timestep {first} is {hosted.times[first]} on the host and {local.times[first]} "
            "locally: the hosted times are not the start of the local times"
        )
    for name in (
        "spec_version",
        "variable",
        "crs",
        "nodata",
        "band_names",
        "mask_variable",
        "coverage_variable",
        "volatility_path",
    ):
        if getattr(hosted, name) != getattr(local, name):
            problems.append(
                f"{name} is {getattr(hosted, name)!r} on the host and "
                f"{getattr(local, name)!r} locally"
            )
    if hosted.bands != local.bands:
        problems.append("the bands (scale, offset or units) differ")
    hosted_levels = hosted.levels or ()
    local_levels = local.levels or ()
    if len(hosted_levels) != len(local_levels):
        problems.append(f"{len(hosted_levels)} levels on the host, {len(local_levels)} locally")
    for theirs, ours in zip(hosted_levels, local_levels, strict=False):
        spatial_theirs = (theirs.path, theirs.resolution, theirs.transform, theirs.shape[1:])
        spatial_ours = (ours.path, ours.resolution, ours.transform, ours.shape[1:])
        if spatial_theirs != spatial_ours or theirs.grid != ours.grid:
            problems.append(
                f"level {ours.path} has another grid (bands, pixel size, transform or chunk "
                f"layout) on the host: {theirs.shape[1:]} against {ours.shape[1:]} pixels"
            )
    return problems


def _named(keys: list[str]) -> str:
    shown = ", ".join(keys[:_LISTED])
    return shown + (f" and {len(keys) - _LISTED} more" if len(keys) > _LISTED else "")


def plan_update(plan: PublishPlan, adapter: StorageAdapter) -> UpdatePlan:
    """Compare the local store with the hosted prefix and list what an update would upload.

    Reads the destination, writes nothing. Raises `PublishError`, listing every reason, unless the
    hosted store is an earlier state of the local one: the same grid, bands and CRS, with a start
    of the local timesteps, and every hosted object either identical or one that an append
    changes in place (`zarr.json`, `time/c/0`, `volatility`).
    """
    if not isinstance(adapter, ReadableStorage):
        raise PublishError(
            f"--update is not available for {adapter.label} yet: it needs to read the hosted "
            "root zarr.json back, and this adapter cannot. Publish to a fresh prefix instead."
        )
    dest = plan.destination
    remote = adapter.list_objects(dest.prefix)
    root_key = dest.key("zarr.json")
    if root_key not in remote:
        what = (
            f"holds {len(remote):,} object(s) but no root zarr.json, which is an interrupted "
            "first upload: run `chronozarr publish` without --update to finish it"
            if remote
            else "is empty: run `chronozarr publish` without --update for a first upload"
        )
        raise PublishError(f"{dest.uri} {what}. --update appends to a store that is published.")
    raw = adapter.read_object(root_key)
    if raw is None:
        raise PublishError(f"{root_key} was listed and then could not be read; run it again.")
    hosted = _chronozarr_block(_read_json(raw, f"{dest.uri}/zarr.json"), f"{dest.uri}/zarr.json")
    local = _chronozarr_block(
        _read_json((plan.store / "zarr.json").read_bytes(), f"{plan.store}/zarr.json"),
        f"{plan.store}/zarr.json",
    )

    problems = _prefix_problems(hosted, local)
    by_key = {dest.key(o.key): o for o in plan.objects}
    foreign = sorted(set(remote) - set(by_key))
    if foreign:
        problems.append(
            f"{len(foreign)} hosted object(s) are not in the local store ({_named(foreign)}): "
            "another timestep range, another layout, or chunks left by an update that was "
            "interrupted and then rolled back locally"
        )
    new: list[LocalObject] = []
    changed: list[LocalObject] = []
    differing_chunks: list[str] = []
    unchanged = 0
    for key, obj in by_key.items():
        if key not in remote:
            new.append(obj)
        elif same_content(obj, remote[key]):
            unchanged += 1
        elif obj.cache_control == SHORT_TTL:
            changed.append(obj)
        else:
            differing_chunks.append(key)
    if differing_chunks:
        problems.append(
            f"{len(differing_chunks)} hosted chunk or shard object(s) differ from the local ones "
            f"({_named(sorted(differing_chunks))}). Chunks are cached for a year and an "
            "unsharded append never rewrites one, so this is another store, a re-encode, or a "
            "sharded append that rewrote its trailing shard"
        )
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        raise PublishError(
            f"{dest.uri} is not an earlier state of {plan.store}; nothing was uploaded.\n"
            f"{listed}\n"
            "Publish the up-to-date copy, or publish this store to a new prefix. `--update` "
            "never replaces or deletes an object that readers may have cached for a year."
        )
    return UpdatePlan(
        plan,
        tuple(new),
        tuple(changed),
        unchanged,
        len(hosted.times),
        len(local.times),
    )


# ---------------------------------------------------------------------------------------------
# Verify that the new length is what a browser gets
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostedRoot:
    n_time: int
    cache_control: str


def read_hosted_root(url: str, timeout: float = 30.0) -> HostedRoot:
    """GET `{url}/zarr.json` as a browser does, asking caches to revalidate (`no-cache`).

    A CDN may answer from its copy anyway; that is what the caller wants to see.
    """
    target = f"{url.rstrip('/')}/zarr.json"
    request = urllib.request.Request(
        target, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(16_000_000)
            control = response.headers.get("Cache-Control", "")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise PublishError(f"GET {target} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise PublishError(f"GET {target} failed: {exc}") from exc
    block = _chronozarr_block(_read_json(body, target), target)
    return HostedRoot(len(block.times), control)


def hosted_root_checks(
    url: str, expected_times: int, fetch: Callable[[str], HostedRoot] = read_hosted_root
) -> list[Check]:
    """Checks that the public root reports `expected_times` and is cached for a short time."""
    try:
        root = fetch(url)
    except PublishError as exc:
        return [
            Check(
                "hosted time length",
                "fail",
                f"could not read the root zarr.json: {exc}",
                "Check the public URL, then run the same command again.",
            )
        ]
    if root.n_time == expected_times:
        length = Check(
            "hosted time length", "ok", f"the public root zarr.json lists {root.n_time} timesteps"
        )
    else:
        length = Check(
            "hosted time length",
            "fail",
            f"the public root zarr.json lists {root.n_time} timesteps, the update has "
            f"{expected_times}",
            "The objects are stored. A cache in front of the bucket still serves the previous "
            f"root, which lives at most {SHORT_TTL_SECONDS} s when it was uploaded with "
            f"`{SHORT_TTL}`. Wait for that, or purge zarr.json at the CDN, then run the same "
            "command again: it uploads nothing and only checks.",
        )
    match = re.search(r"max-age=(\d+)", root.cache_control)
    lifetime = int(match.group(1)) if match else None
    if "immutable" in root.cache_control or (lifetime or 0) > SHORT_TTL_SECONDS:
        control = Check(
            "root cache-control",
            "warn",
            f"{root.cache_control}: caches may keep serving an old root for that long",
            f"Upload the root with `{SHORT_TTL}` so that new timesteps show within "
            f"{SHORT_TTL_SECONDS} s. `chronozarr publish --update` does this for every zarr.json "
            "it uploads; one it did not upload keeps its old header.",
        )
    else:
        control = Check(
            "root cache-control",
            "ok" if lifetime is not None else "info",
            root.cache_control or "absent: caches apply their own default lifetime",
        )
    return [length, control]


def publish_update(
    update: UpdatePlan,
    adapter: StorageAdapter,
    *,
    apply_cors: bool = False,
    workers: int = 16,
    log: Log = lambda _: None,
    check_store: Callable[[str], list[Check]] = lambda url: diagnose(url, origin=DEFAULT_ORIGIN),
    fetch_root: Callable[[str], HostedRoot] = read_hosted_root,
) -> PublishResult:
    """Upload the new and changed objects, then verify the hosted store.

    The link is returned only when the doctor checks pass and the public root lists the new
    number of timesteps. It is the link of the same public URL as before.
    """
    plan = update.plan
    todo = list(update.todo)
    stored = put_objects(todo, plan.destination, adapter, workers=workers, log=log)
    report = UploadReport(stored, update.unchanged, sum(o.size for o in todo))
    log(
        f"stored {report.uploaded:,} objects ({report.uploaded_bytes:,} bytes), "
        f"{report.skipped:,} already present"
    )
    return verify_hosted(
        plan,
        report,
        adapter,
        apply_cors,
        check_store,
        log,
        extra_checks=lambda: hosted_root_checks(plan.public_url, update.local_times, fetch_root),
    )
