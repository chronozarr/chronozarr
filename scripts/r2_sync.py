"""Upload or delete chronozarr store prefixes in R2 through its S3 API, many objects at a time.

wrangler uploads one object per process at about two per second, which is fine for a sharded store
(93 objects) and hopeless for an unsharded one (about 5,900 objects per 117-month imagery store,
17,000 with mask and coverage planes). This script uses boto3 with a thread pool and skips objects
the bucket already holds at the same size, so it can be re-run after an interruption.

Credentials: an R2 API token with Object Read & Write on the bucket, as the environment variables
R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY (dashboard: R2 > Manage R2 API Tokens). The endpoint is
https://<account id>.r2.cloudflarestorage.com, from R2_ENDPOINT or --endpoint.

Cache-Control follows docs/hosting.md: every zarr.json object, each level's time/c/0 and the
volatility chunk get max-age=300 (they change on append); every other object is immutable.

    uv run --with boto3 python scripts/r2_sync.py upload ucayali_santa_maria/png-v03
    uv run --with boto3 python scripts/r2_sync.py upload ucayali_santa_maria/png-v03 --dry-run
    uv run --with boto3 python scripts/r2_sync.py delete <aoi>/<retired-store>   # prints counts; add --yes to delete
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "stores"
DEFAULT_BUCKET = "chronozarr-stores"
DEFAULT_ENDPOINT = "https://c92bd6376ca1cda03e137c3e21ab5272.r2.cloudflarestorage.com"
SHORT_TTL = "public, max-age=300"
IMMUTABLE = "public, max-age=31536000, immutable"


def cache_control(key: str) -> str:
    """Short lifetime for the objects an append rewrites in place; immutable for the rest."""
    name = key.rsplit("/", 1)[-1]
    if name == "zarr.json":
        return SHORT_TTL
    if key.endswith("/time/c/0") or "/volatility/c/" in key:
        return SHORT_TTL
    return IMMUTABLE


def content_type(key: str) -> str:
    return "application/json" if key.endswith("zarr.json") else "application/octet-stream"


def local_objects(prefix: str) -> dict[str, Path]:
    """Every file under data/stores/<prefix>, keyed by its bucket key."""
    store = ROOT / prefix
    if not (store / "zarr.json").is_file():
        sys.exit(f"{store} is not a chronozarr store (no zarr.json); prefix must be <aoi>/<store>")
    return {
        f"{prefix}/{p.relative_to(store).as_posix()}": p
        for p in sorted(store.rglob("*"))
        if p.is_file()
    }


def client(endpoint: str):
    try:
        import boto3  # ty: ignore[unresolved-import]  # optional: supplied by `uv run --with boto3`
    except ImportError:
        sys.exit("boto3 is missing: run with `uv run --with boto3 python scripts/r2_sync.py ...`")
    key_id = os.environ.get("R2_ACCESS_KEY_ID")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not key_id or not secret:
        sys.exit(
            "set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY (an R2 API token, Object Read & Write)"
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="auto",
    )


def remote_objects(s3, bucket: str, prefix: str) -> dict[str, int]:
    """Keys and sizes already under the prefix."""
    found: dict[str, int] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            found[obj["Key"]] = obj["Size"]
    return found


def upload(args: argparse.Namespace) -> None:
    files = local_objects(args.prefix)
    total_bytes = sum(p.stat().st_size for p in files.values())
    print(f"{args.prefix}: {len(files)} objects, {total_bytes / 1e6:.1f} MB locally")
    if args.dry_run:
        short = sum(1 for k in files if cache_control(k) == SHORT_TTL)
        print(
            f"dry run: {short} short-lived objects, {len(files) - short} immutable; nothing sent"
        )
        return
    s3 = client(args.endpoint)
    existing = remote_objects(s3, args.bucket, args.prefix)
    todo = [(k, p) for k, p in files.items() if existing.get(k) != p.stat().st_size]
    print(f"{len(existing)} already in the bucket, {len(todo)} to upload")
    if not todo:
        return

    def put(key: str, path: Path) -> str:
        s3.put_object(
            Bucket=args.bucket,
            Key=key,
            Body=path.read_bytes(),
            CacheControl=cache_control(key),
            ContentType=content_type(key),
        )
        return key

    started = time.monotonic()
    done = 0
    failed: list[tuple[str, Exception]] = []
    # Chunks first, metadata last, so a reader never sees a root that lists objects not yet there.
    todo.sort(key=lambda kp: (cache_control(kp[0]) == SHORT_TTL, kp[0]))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(put, k, p): k for k, p in todo}
        for future in as_completed(futures):
            try:
                future.result()
                done += 1
            except Exception as error:  # report every failure and keep going
                failed.append((futures[future], error))
            if (done + len(failed)) % 500 == 0:
                rate = (done + len(failed)) / (time.monotonic() - started)
                print(f"  {done + len(failed)} / {len(todo)} ({rate:.0f} objects/s)")
    elapsed = time.monotonic() - started
    rate = done / max(elapsed, 1e-9)
    print(f"uploaded {done} objects in {elapsed:.0f} s ({rate:.0f}/s); {len(failed)} failed")
    for key, error in failed[:20]:
        print(f"  FAIL {key}: {error}")
    if failed:
        sys.exit(1)


def delete(args: argparse.Namespace) -> None:
    s3 = client(args.endpoint)
    existing = remote_objects(s3, args.bucket, args.prefix)
    size = sum(existing.values())
    print(f"{args.prefix}: {len(existing)} objects, {size / 1e6:.1f} MB in the bucket")
    if not existing:
        return
    if not args.yes:
        sys.exit("refusing to delete without --yes")
    keys = sorted(existing)
    for start in range(0, len(keys), 1000):
        batch = keys[start : start + 1000]
        response = s3.delete_objects(
            Bucket=args.bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True}
        )
        errors = response.get("Errors", [])
        if errors:
            sys.exit(f"delete failed for {len(errors)} objects, first: {errors[0]}")
        print(f"  deleted {min(start + 1000, len(keys))} / {len(keys)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["upload", "delete"])
    parser.add_argument(
        "prefix",
        help="<aoi>/<store>, a directory under data/stores and the key prefix in the bucket",
    )
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--endpoint", default=os.environ.get("R2_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--workers", type=int, default=32, help="parallel uploads (default 32)")
    parser.add_argument(
        "--dry-run", action="store_true", help="upload: list and classify the local objects only"
    )
    parser.add_argument("--yes", action="store_true", help="delete: confirm")
    args = parser.parse_args()
    if args.action == "upload":
        upload(args)
    else:
        delete(args)


if __name__ == "__main__":
    main()
