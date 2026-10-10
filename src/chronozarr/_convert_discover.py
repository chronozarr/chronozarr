"""Find a GeoTIFF time series in a directory, glob or S3 prefix and date it from the file names.

Discovery only lists files and reads dates from their names; the converter's own checks (grid,
bands, scale, offset, nodata) run afterwards on the entries it produces. A name is dated only when
it holds exactly one date, so an ambiguous name is reported and never guessed at.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from chronozarr._convert_manifest import Entry
from chronozarr._convert_preflight import Problem
from chronozarr._convert_xarray import NETCDF_SUFFIXES

TIFF_SUFFIXES = (".tif", ".tiff")
GLOB_CHARS = "*?["
_ZARR_MARKERS = ("zarr.json", ".zgroup", ".zarray", ".zmetadata")
_YEARS = range(1900, 2101)

# YYYYMMDD, YYYY-MM-DD or YYYY_MM_DD (one separator style); and YYYY-MM or YYYY_MM. A bare
# six-digit YYYYMM is not read: it cannot be told from YYMMDD. `--date-pattern` reads it.
_DAY = re.compile(r"(?<!\d)(\d{4})([-_]?)(\d{2})\2(\d{2})(?!\d)")
_MONTH = re.compile(r"(?<!\d)(\d{4})[-_](\d{2})(?!\d)")

_DIRECTIVES = {
    "Y": r"(?P<Y>\d{4})",
    "m": r"(?P<m>\d{2})",
    "d": r"(?P<d>\d{2})",
    "H": r"(?P<H>\d{2})",
    "M": r"(?P<M>\d{2})",
    "S": r"(?P<S>\d{2})",
}

DATE_HELP = (
    "name the date YYYYMMDD, YYYY-MM-DD or YYYY-MM, or pass --date-pattern with the text around "
    "the date (for example 'ndvi_%Y%m%d'), or write a manifest"
)


def is_discovery_source(text: str) -> bool:
    """Whether `text` names GeoTIFFs to discover: an s3:// prefix, a glob, a directory that is not
    a Zarr store, or a single .tif file. Manifests, NetCDF files, Zarr stores and URLs are not."""
    lowered = text.lower()
    if lowered.endswith((".csv", ".json", ".zarr", *NETCDF_SUFFIXES)):
        return False
    if lowered.startswith("s3://"):
        return True
    if "://" in text:
        return False
    path = Path(text).expanduser()
    if path.is_dir():
        return not any((path / marker).exists() for marker in _ZARR_MARKERS)
    if path.is_file():
        return lowered.endswith(TIFF_SUFFIXES)
    return any(ch in text for ch in GLOB_CHARS)


# --- listing ------------------------------------------------------------------------------------


def list_tiffs(spec: str) -> list[str]:
    """The .tif/.tiff files of `spec`: sorted absolute paths, or s3:// URIs for an S3 prefix.

    `spec` is a directory (not searched recursively), a glob (`dir/**/*.tif` recurses), an S3
    prefix `s3://bucket/path/` (not recursive) or one file. Raises ValueError when nothing is
    found or the location cannot be listed.
    """
    if spec.lower().startswith("s3://"):
        return _list_s3(spec)
    return _list_local(spec)


def _list_local(spec: str) -> list[str]:
    path = Path(spec).expanduser()
    if path.is_dir():
        found = [p for p in path.iterdir() if p.is_file()]
        where = f"in {path}"
    elif path.is_file():
        found = [path]
        where = f"at {path}"
    elif any(ch in spec for ch in GLOB_CHARS):
        found = [Path(m) for m in glob.glob(str(path), recursive=True) if Path(m).is_file()]
        where = f"matching '{spec}'"
    else:
        raise ValueError(f"{spec} does not exist")
    tiffs = sorted(str(p.resolve()) for p in found if p.suffix.lower() in TIFF_SUFFIXES)
    if not tiffs:
        raise ValueError(
            f"no GeoTIFF files (.tif, .tiff) found {where}. Check the path; quote a glob so the "
            "shell does not expand it; a directory is not searched recursively (use "
            "'dir/**/*.tif' for subdirectories)"
        )
    return tiffs


def _list_s3(spec: str) -> list[str]:
    bucket, _, key = spec[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"'{spec}' needs a bucket: s3://bucket/path/")
    if any(ch in spec for ch in GLOB_CHARS):
        raise ValueError(
            f"'{spec}': an S3 source is a prefix such as s3://bucket/path/, not a glob. Name the "
            "prefix that holds the files, or write a manifest of s3:// URIs"
        )
    if key.lower().endswith(TIFF_SUFFIXES):
        return [spec]
    prefix = key if not key or key.endswith("/") else f"{key}/"
    tiffs = sorted(
        f"s3://{bucket}/{k}"
        for k in _list_s3_keys(bucket, prefix)
        if k.lower().endswith(TIFF_SUFFIXES)
    )
    if not tiffs:
        raise ValueError(
            f"no GeoTIFF files (.tif, .tiff) found under s3://{bucket}/{prefix}. Check the "
            "bucket and prefix (a prefix is not searched recursively)"
        )
    return tiffs


def _list_s3_keys(bucket: str, prefix: str) -> list[str]:
    """Object keys directly under `prefix`, with the caller's AWS credential chain.

    The network boundary of discovery. `AWS_NO_SIGN_REQUEST=YES` lists a public bucket without
    credentials, the same switch GDAL reads for `s3://` objects.
    """
    try:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError as exc:
        raise ValueError(
            "listing an S3 prefix needs boto3: run `uv sync --extra s3` "
            "(or `pip install 'chronozarr[s3]'`), or write a manifest of s3:// URIs instead"
        ) from exc
    unsigned = os.environ.get("AWS_NO_SIGN_REQUEST", "").upper() in ("YES", "TRUE", "ON", "1")
    where = f"s3://{bucket}/{prefix}"
    try:
        client = boto3.client(
            "s3", config=Config(signature_version=UNSIGNED) if unsigned else None
        )
        keys: list[str] = []
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, Delimiter="/"
        )
        for page in pages:
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys
    except NoCredentialsError as exc:
        raise ValueError(
            f"cannot list {where}: no AWS credentials found. Configure the AWS credential chain "
            "(AWS_ACCESS_KEY_ID, AWS_PROFILE, ~/.aws/credentials or SSO), or set "
            "AWS_NO_SIGN_REQUEST=YES for a public bucket"
        ) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "error")
        raise ValueError(
            f"cannot list {where}: {code}. Check the bucket name and prefix, and that your "
            "credentials allow s3:ListBucket on the bucket"
        ) from exc
    except BotoCoreError as exc:
        raise ValueError(f"cannot list {where}: {exc}") from exc


# --- dates in file names --------------------------------------------------------------------


def _moment(year: int, month: int, day: int, hour: int, minute: int, second: int) -> np.datetime64:
    """The datetime as UTC milliseconds; ValueError for an impossible date or implausible year."""
    if year not in _YEARS:
        raise ValueError(f"year {year} is outside {_YEARS.start}..{_YEARS.stop - 1}")
    return np.datetime64(datetime(year, month, day, hour, minute, second), "ms")


def _compile_pattern(date_pattern: str) -> re.Pattern[str]:
    """A strptime-like `date_pattern` (%Y %m %d %H %M %S, %% for a percent sign) as a regex."""
    parts: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(date_pattern):
        char = date_pattern[i]
        if char != "%":
            parts.append(re.escape(char))
            i += 1
            continue
        directive = date_pattern[i + 1 : i + 2]
        if directive == "%":
            parts.append("%")
        elif directive in _DIRECTIVES and directive not in seen:
            seen.add(directive)
            parts.append(_DIRECTIVES[directive])
        else:
            raise ValueError(
                f"--date-pattern '{date_pattern}': '%{directive}' is not supported or repeated; "
                "use %Y %m %d %H %M %S once each (for example '%Y%m%d' or 'ndvi_%Y-%m')"
            )
        i += 2
    if not {"Y", "m"} <= seen:
        raise ValueError(f"--date-pattern '{date_pattern}' needs at least %Y and %m")
    head = r"(?<!\d)" if date_pattern.startswith("%") and date_pattern[1:2] in _DIRECTIVES else ""
    tail = r"(?!\d)" if date_pattern.endswith(tuple(f"%{d}" for d in _DIRECTIVES)) else ""
    return re.compile(head + "".join(parts) + tail)


def find_dates(
    stem: str, pattern: re.Pattern[str] | None
) -> tuple[dict[np.datetime64, str], list[str]]:
    """The distinct dates in a file name stem, each with the text it was read from, and the texts
    that matched but are not real dates.

    Without `pattern`: YYYYMMDD, YYYY-MM-DD, YYYY_MM_DD (day) and YYYY-MM, YYYY_MM (first of the
    month); numbers that are no calendar date are not dates. With `pattern`: what it matches, and a
    match that is no calendar date is reported as invalid.
    """
    found: dict[np.datetime64, str] = {}
    invalid: list[str] = []
    if pattern is not None:
        for match in pattern.finditer(stem):
            fields = {k: int(v) for k, v in match.groupdict().items()}
            try:
                moment = _moment(
                    fields["Y"],
                    fields["m"],
                    fields.get("d", 1),
                    fields.get("H", 0),
                    fields.get("M", 0),
                    fields.get("S", 0),
                )
            except ValueError:
                invalid.append(match.group(0))
                continue
            found.setdefault(moment, match.group(0))
        return found, invalid
    covered: list[tuple[int, int]] = []
    for match in _DAY.finditer(stem):
        year, _, month, day = match.groups()
        try:
            moment = _moment(int(year), int(month), int(day), 0, 0, 0)
        except ValueError:
            continue
        covered.append(match.span())
        found.setdefault(moment, match.group(0))
    for match in _MONTH.finditer(stem):
        if any(start < match.end() and match.start() < end for start, end in covered):
            continue
        try:
            moment = _moment(int(match.group(1)), int(match.group(2)), 1, 0, 0, 0)
        except ValueError:
            continue
        found.setdefault(moment, match.group(0))
    return found, invalid


def show_time(moment: np.datetime64) -> str:
    midnight = moment == moment.astype("datetime64[D]")
    return str(np.datetime_as_string(moment, unit="D" if midnight else "s"))


def _file_name_stem(uri: str) -> str:
    return uri.rsplit("/", 1)[-1].rsplit(".", 1)[0]


@dataclass(frozen=True)
class Discovery:
    """GeoTIFFs found by `discover`, dated from their names.

    `entries` are the files with exactly one date, by time (two files may share one: those are in
    `problems`). `undated` have no date or several. `problems` are the date findings of every
    file: missing, ambiguous, impossible or duplicate; empty when the series is dated cleanly.
    """

    entries: list[Entry]
    undated: list[str]
    problems: list[Problem]


def discover(spec: str, date_pattern: str | None = None) -> Discovery:
    """List the GeoTIFFs of `spec` and read a date from each file name.

    Raises ValueError when nothing is found, the location cannot be listed or `date_pattern` is
    malformed. Date problems are returned, not raised, so they can be reported together with the
    problems the converter finds in the files themselves.
    """
    pattern = None if date_pattern is None else _compile_pattern(date_pattern)
    dated: list[Entry] = []
    undated: list[str] = []
    problems: list[Problem] = []
    for uri in list_tiffs(spec):
        found, invalid = find_dates(_file_name_stem(uri), pattern)
        if invalid:
            undated.append(uri)
            problems.append(
                Problem(
                    uri,
                    f"'{invalid[0]}' in the file name matches --date-pattern '{date_pattern}' but "
                    "is not a real date",
                    "rename the file, or exclude it by narrowing the glob",
                )
            )
        elif not found:
            undated.append(uri)
            if date_pattern is None:
                problems.append(Problem(uri, "no date in the file name", DATE_HELP))
            else:
                problems.append(
                    Problem(
                        uri,
                        f"the file name does not match --date-pattern '{date_pattern}'",
                        "rename the file, narrow the glob to exclude it, or write a manifest",
                    )
                )
        elif len(found) > 1:
            undated.append(uri)
            texts = ", ".join(f"'{text}' ({show_time(t)})" for t, text in sorted(found.items()))
            problems.append(
                Problem(
                    uri,
                    f"the file name has several dates, so it cannot be dated: {texts}",
                    "pass --date-pattern with the text around the date to use (for example "
                    "'composite_%Y%m%d'), or write a manifest",
                )
            )
        else:
            ((moment, _),) = found.items()
            dated.append(Entry(uri, moment))
    dated.sort(key=lambda e: (e.time, e.uri))
    by_time: dict[np.datetime64, list[str]] = defaultdict(list)
    for entry in dated:
        by_time[entry.time].append(entry.uri)
    for moment, uris in by_time.items():
        if len(uris) == 1:
            continue
        for uri in uris:
            others = ", ".join(u for u in uris if u != uri)
            problems.append(
                Problem(
                    uri,
                    f"has the same date {show_time(moment)} as {others}; a store has one timestep "
                    "per time",
                    "leave one out (narrow the glob), or if the names carry a time of day pass "
                    "--date-pattern with %H%M%S, or write a manifest with distinct datetimes",
                )
            )
    return Discovery(dated, undated, problems)


# --- manifest export --------------------------------------------------------------------------


def check_manifest_target(path: Path) -> None:
    """Fail before any work when `path` cannot receive a manifest: wrong suffix, or it exists."""
    if path.suffix.lower() not in (".csv", ".json"):
        raise ValueError(f"--write-manifest {path}: a manifest must be .csv or .json")
    if path.exists():
        raise FileExistsError(f"{path} already exists; choose a new path or remove it first")


def write_manifest(entries: list[Entry], path: Path) -> None:
    """Write `entries` as a manifest `convert` reads back: CSV `uri,datetime` or a JSON list."""
    check_manifest_target(path)
    rows = [
        {
            "uri": e.uri,
            "datetime": np.datetime_as_string(
                e.time, unit="D" if e.time == e.time.astype("datetime64[D]") else "ms"
            ),
        }
        for e in entries
    ]
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer: Any = csv.DictWriter(handle, fieldnames=["uri", "datetime"])
        writer.writeheader()
        writer.writerows(rows)
