"""Find a raster time series in a directory, glob or S3 prefix and date it from the file names.

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
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from chronozarr._convert_manifest import Entry
from chronozarr._convert_preflight import Problem
from chronozarr._convert_xarray import NETCDF_SUFFIXES
from chronozarr._install import extra_hint

TIFF_SUFFIXES = (".tif", ".tiff")
# Files GDAL reads as a raster, which a directory, glob or S3 prefix is searched for. The sidecars
# that go with them (.hdr, .prj, .aux.xml, .stx, .xml, .txt, .csv) are not rasters, and NetCDF has
# its own route (a single .nc file with --variable), so none of these is listed.
RASTER_SUFFIXES = (
    *TIFF_SUFFIXES,
    ".bil",
    ".flt",
    ".img",
    ".asc",
    ".grib",
    ".grib2",
    ".grb",
    ".grb2",
    ".hdf",
    ".h5",
    ".hdf5",
    ".he5",
    ".jp2",
    ".vrt",
)
ZIP_SUFFIX = ".zip"  # a local zip that holds one raster is a source too
GLOB_CHARS = "*?["
_ZARR_MARKERS = ("zarr.json", ".zgroup", ".zarray", ".zmetadata")
_YEARS = range(1900, 2101)

# YYYYMMDD, YYYY-MM-DD or YYYY_MM_DD (one separator style); and YYYY-MM or YYYY_MM. A bare
# six-digit YYYYMM is not read: it cannot be told from YYMMDD. `--date-pattern` reads it.
_DAY = re.compile(r"(?<!\d)(\d{4})([-_]?)(\d{2})\2(\d{2})(?!\d)")
_MONTH = re.compile(r"(?<!\d)(\d{4})[-_](\d{2})(?!\d)")

# A time of day after a date, as HLS names its files ('2020061T155151').
_TIME_OF_DAY = re.compile(r"T\d{6}(?!\d)")

_DIRECTIVES = {
    "Y": r"(?P<Y>\d{4})",
    "m": r"(?P<m>\d{2})",
    "d": r"(?P<d>\d{2})",
    "H": r"(?P<H>\d{2})",
    "M": r"(?P<M>\d{2})",
    "S": r"(?P<S>\d{2})",
    "j": r"(?P<j>\d{3})",
}

DATE_HELP = (
    "name the date YYYYMMDD, YYYY-MM-DD or YYYY-MM, or pass --date-pattern with the text around "
    "the date (for example 'ndvi_%Y%m%d'). A date with a month name, such as Jan2024, needs a "
    "manifest: a CSV with the columns uri,datetime and one row per file, such as "
    "'scene_Jan2024.tif,2024-01-01'"
)


def is_discovery_source(text: str) -> bool:
    """Whether `text` names rasters to discover: an s3:// prefix, a glob, a directory that is not
    a Zarr store, or a single raster or zip file. Manifests, NetCDF files, Zarr stores and URLs
    are not."""
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
        return lowered.endswith((*RASTER_SUFFIXES, ZIP_SUFFIX))
    return any(ch in text for ch in GLOB_CHARS)


# --- listing ------------------------------------------------------------------------------------


def list_rasters(spec: str) -> list[str]:
    """The raster files of `spec` (`RASTER_SUFFIXES`, and local `.zip` files): sorted absolute
    paths, or s3:// URIs for an S3 prefix.

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
    rasters = sorted(
        str(p.resolve()) for p in found if p.suffix.lower() in (*RASTER_SUFFIXES, ZIP_SUFFIX)
    )
    if not rasters:
        raise ValueError(
            f"no raster files found {where}. Looked for "
            f"{_suffix_list((*RASTER_SUFFIXES, ZIP_SUFFIX))} files. Check the path; quote a glob "
            "so the shell does not expand it; a directory is not searched recursively (use "
            "'dir/**/*.tif' for subdirectories). Files with another extension can be listed in a "
            "manifest"
        )
    return rasters


def _suffix_list(suffixes: Sequence[str]) -> str:
    return f"{', '.join(suffixes[:-1])} and {suffixes[-1]}"


def _list_s3(spec: str) -> list[str]:
    bucket, _, key = spec[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"'{spec}' needs a bucket: s3://bucket/path/")
    if any(ch in spec for ch in GLOB_CHARS):
        raise ValueError(
            f"'{spec}': an S3 source is a prefix such as s3://bucket/path/, not a glob. Name the "
            "prefix that holds the files, or write a manifest of s3:// URIs"
        )
    if key.lower().endswith(RASTER_SUFFIXES):
        return [spec]
    prefix = key if not key or key.endswith("/") else f"{key}/"
    rasters = sorted(
        f"s3://{bucket}/{k}"
        for k in _list_s3_keys(bucket, prefix)
        if k.lower().endswith(RASTER_SUFFIXES)
    )
    if not rasters:
        raise ValueError(
            f"no raster files found under s3://{bucket}/{prefix}. Looked for "
            f"{_suffix_list(RASTER_SUFFIXES)} files (zips are read from local folders only). "
            "Check the bucket and prefix (a prefix is not searched recursively)"
        )
    return rasters


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
            f"listing an S3 prefix needs boto3: {extra_hint('s3')}; or write a manifest of s3:// "
            "URIs instead"
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


# --- zips -----------------------------------------------------------------------------------


def zip_raster(path: str) -> str:
    """The member of the local zip `path` that is a raster, when the zip holds exactly one.

    Its sidecars (.hdr, .prj, .aux.xml, ...) are not rasters, so a zip of one BIL file and its
    sidecars is one raster. Raises ValueError, in words for a person, when `path` is not a
    readable local zip or holds no raster or several.
    """
    if "://" in path:
        raise ValueError(
            "a zip is read from a local folder only; download and unzip it, then convert the "
            "folder"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"not a readable zip file ({exc})") from exc
    # macOS adds `__MACOSX/._name` resource forks next to every file it zips
    names = [
        n for n in names if not n.startswith("__MACOSX/") and not Path(n).name.startswith("._")
    ]
    rasters = [n for n in names if n.lower().endswith(RASTER_SUFFIXES)]
    if not rasters:
        raise ValueError(
            f"the zip holds no raster ({len(names)} file{'s' if len(names) != 1 else ''}, none "
            f"ending in {_suffix_list(RASTER_SUFFIXES)})"
        )
    if len(rasters) > 1:
        shown = ", ".join(rasters[:4]) + (
            f" and {len(rasters) - 4} more" if len(rasters) > 4 else ""
        )
        raise ValueError(
            f"the zip holds {len(rasters)} rasters ({shown}); chronozarr reads a zip that holds "
            "exactly one. Unzip it and convert the folder"
        )
    return rasters[0]


# --- dates in file names --------------------------------------------------------------------


def _moment(year: int, month: int, day: int, hour: int, minute: int, second: int) -> np.datetime64:
    """The datetime as UTC milliseconds; ValueError for an impossible date or implausible year."""
    if year not in _YEARS:
        raise ValueError(f"year {year} is outside {_YEARS.start}..{_YEARS.stop - 1}")
    return np.datetime64(datetime(year, month, day, hour, minute, second), "ms")


def _month_and_day(year: int, day_of_year: int) -> tuple[int, int]:
    """The month and day of `day_of_year` (1-366) in `year`; ValueError when the year has no such
    day. `strptime` is not used: it wraps day 366 of a common year into January of the next."""
    if not 1 <= day_of_year <= 366:
        raise ValueError(f"day of year {day_of_year} is outside 1..366")
    day = date(year, 1, 1) + timedelta(days=day_of_year - 1)
    if day.year != year:
        raise ValueError(f"{year} has no day {day_of_year}")
    return day.month, day.day


def _compile_pattern(date_pattern: str) -> re.Pattern[str]:
    """A strptime-like `date_pattern` as a regex.

    Directives: %Y %m %d %H %M %S once each, %j (day of year) with %Y instead of %m and %d, and %%
    for a percent sign.
    """
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
                "use %Y %m %d %j %H %M %S once each (for example '%Y%m%d', '%Y%j' or 'ndvi_%Y-%m')"
            )
        i += 2
    if "j" in seen and seen & {"m", "d"}:
        raise ValueError(
            f"--date-pattern '{date_pattern}': %j (day of year) cannot be combined with %m or %d; "
            "use %Y%j for a day of year, or %Y%m%d for a month and day"
        )
    if not ({"Y", "m"} <= seen or {"Y", "j"} <= seen):
        raise ValueError(f"--date-pattern '{date_pattern}' needs at least %Y and %m, or %Y and %j")
    head = r"(?<!\d)" if date_pattern.startswith("%") and date_pattern[1:2] in _DIRECTIVES else ""
    tail = r"(?!\d)" if date_pattern.endswith(tuple(f"%{d}" for d in _DIRECTIVES)) else ""
    return re.compile(head + "".join(parts) + tail)


def _date_spans(
    text: str, pattern: re.Pattern[str] | None
) -> tuple[list[tuple[int, int, np.datetime64]], list[str]]:
    """Where `text` holds a date, as (start, end, moment), and the texts that matched `pattern` but
    are not real dates (a month 13, a day 366 in a common year). Without `pattern`, the default
    rules: YYYYMMDD, YYYY-MM-DD, YYYY_MM_DD (day) and YYYY-MM, YYYY_MM (first of the month)."""
    if pattern is None:
        return _default_date_spans(text), []
    spans: list[tuple[int, int, np.datetime64]] = []
    invalid: list[str] = []
    for match in pattern.finditer(text):
        fields = {k: int(v) for k, v in match.groupdict().items()}
        try:
            if "j" in fields:
                month, day_of_month = _month_and_day(fields["Y"], fields["j"])
            else:
                month, day_of_month = fields["m"], fields.get("d", 1)
            moment = _moment(
                fields["Y"],
                month,
                day_of_month,
                fields.get("H", 0),
                fields.get("M", 0),
                fields.get("S", 0),
            )
        except ValueError:
            invalid.append(match.group(0))
            continue
        spans.append((*match.span(), moment))
    return spans, invalid


def find_dates(
    stem: str, pattern: re.Pattern[str] | None
) -> tuple[dict[np.datetime64, str], list[str]]:
    """The distinct dates in a file name stem, each with the text it was read from, and the texts
    that matched but are not real dates.

    Without `pattern`: YYYYMMDD, YYYY-MM-DD, YYYY_MM_DD (day) and YYYY-MM, YYYY_MM (first of the
    month); numbers that are no calendar date are not dates. With `pattern`: what it matches, and a
    match that is no calendar date (or no such day of year) is reported as invalid.
    """
    spans, invalid = _date_spans(stem, pattern)
    found: dict[np.datetime64, str] = {}
    for start, end, moment in spans:
        found.setdefault(moment, stem[start:end])
    return found, invalid


def _default_date_spans(text: str) -> list[tuple[int, int, np.datetime64]]:
    """Where `text` holds a date by the default rules: (start, end, moment), days before months."""
    spans: list[tuple[int, int, np.datetime64]] = []
    covered: list[tuple[int, int]] = []
    for match in _DAY.finditer(text):
        year, _, month, day = match.groups()
        try:
            moment = _moment(int(year), int(month), int(day), 0, 0, 0)
        except ValueError:
            continue
        covered.append(match.span())
        spans.append((*match.span(), moment))
    for match in _MONTH.finditer(text):
        if any(start < match.end() and match.start() < end for start, end in covered):
            continue
        try:
            moment = _moment(int(match.group(1)), int(match.group(2)), 1, 0, 0, 0)
        except ValueError:
            continue
        spans.append((*match.span(), moment))
    return spans


DATE_MARK = "\x00"


def mask_dates(text: str, pattern: re.Pattern[str] | None = None) -> str:
    """`text` with every date read from it (by `pattern`, else the default rules) replaced by
    `DATE_MARK`: the part of a band description or file name that is the same from file to file
    once the date is taken out."""
    spans, _ = _date_spans(text, pattern)
    for start, end, _ in sorted(spans, key=lambda span: span[0], reverse=True):
        text = text[:start] + DATE_MARK + text[end:]
    return text


def year_and_day_of_year_help(stem: str) -> str | None:
    """The fix for a name that holds one year and day of year, such as 'MOD13A1.A2020001.h12v04',
    or None.

    The default rules do not read YYYYDDD: it cannot be told from any other seven digits. This
    suggests the `--date-pattern` that does: the text before the date, `%Y%j`, and `T%H%M%S` when
    a time of day follows. The pattern is run on the name first, so it is only suggested when it
    reads exactly one date. A name with two different candidates is not guessed at.
    """
    bare = _compile_pattern("%Y%j")
    candidates, _ = find_dates(stem, bare)
    if len(candidates) != 1:
        return None
    ((_, digits),) = candidates.items()
    match = next(m for m in bare.finditer(stem) if m.group(0) == digits)
    prefix = stem[: match.start()].replace("%", "%%")
    clock = _TIME_OF_DAY.match(stem, match.end())
    attempts = [("%Y%j", digits)]
    if clock is not None:
        attempts.insert(0, ("%Y%jT%H%M%S", digits + clock.group(0)))
    for directives, token in attempts:
        pattern = prefix + directives
        dates, invalid = find_dates(stem, _compile_pattern(pattern))
        if invalid or len(dates) != 1:
            continue
        (moment,) = dates
        quoted = "'" + pattern.replace("'", "'\\''") + "'"
        return (
            f"the name has a year and day of year, '{token}' ({show_time(moment)}): pass "
            f"--date-pattern {quoted} to read it. Otherwise {DATE_HELP}"
        )
    return None


def show_time(moment: np.datetime64) -> str:
    midnight = moment == moment.astype("datetime64[D]")
    return str(np.datetime_as_string(moment, unit="D" if midnight else "s"))


def _file_name_stem(uri: str) -> str:
    return uri.rsplit("/", 1)[-1].rsplit(".", 1)[0]


@dataclass(frozen=True)
class Discovery:
    """Rasters found by `discover`, dated from their names.

    `entries` are the files with exactly one date, by time and name. Several files may share a
    date: whether they are tiles of one image or copies takes their grids, which the converter
    reads (`_convert_tiles`). `undated` have no date or several. `problems` are the date findings
    of every file: missing, ambiguous or impossible; empty when every name is dated cleanly.
    """

    entries: list[Entry]
    undated: list[str]
    problems: list[Problem]


def _dates_in_zipped_raster(
    uri: str, pattern: re.Pattern[str] | None
) -> tuple[dict[np.datetime64, str], list[str]]:
    """`find_dates` on the name of the raster inside the zip `uri`; nothing when the zip does not
    hold exactly one (opening the zip reports that)."""
    try:
        member = zip_raster(uri)
    except ValueError:
        return {}, []
    return find_dates(_file_name_stem(member), pattern)


def discover(spec: str, date_pattern: str | None = None) -> Discovery:
    """List the rasters of `spec` and read a date from each file name.

    A zip is dated from its own name, or from the name of the raster inside it when its own holds
    no date. Raises ValueError when nothing is found, the location cannot be listed or
    `date_pattern` is malformed. Date problems are returned, not raised, so they can be reported
    together with the problems the converter finds in the files themselves.
    """
    pattern = None if date_pattern is None else _compile_pattern(date_pattern)
    dated: list[Entry] = []
    undated: list[str] = []
    problems: list[Problem] = []
    for uri in list_rasters(spec):
        zipped = uri.lower().endswith(ZIP_SUFFIX)
        found, invalid = find_dates(_file_name_stem(uri), pattern)
        if zipped and not found and not invalid:
            found, invalid = _dates_in_zipped_raster(uri, pattern)
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
                fix = year_and_day_of_year_help(_file_name_stem(uri)) or DATE_HELP
                what = (
                    "the name of the zip or of the raster inside it" if zipped else "the file name"
                )
                problems.append(Problem(uri, f"no date in {what}", fix))
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
    return Discovery(dated, undated, problems)


# --- manifest export --------------------------------------------------------------------------


def check_manifest_target(path: Path) -> None:
    """Fail before any work when `path` cannot receive a manifest: wrong suffix, or it exists."""
    if path.suffix.lower() not in (".csv", ".json"):
        raise ValueError(f"--write-manifest {path}: a manifest must be .csv or .json")
    if path.exists():
        raise FileExistsError(f"{path} already exists; choose a new path or remove it first")


def write_manifest(
    entries: list[Entry],
    path: Path,
    *,
    undated: Sequence[str] = (),
    bands: Sequence[str] | None = None,
) -> None:
    """Write `entries` as a manifest `convert` reads back: CSV `uri,datetime[,bands]` or JSON.

    `undated` files get a row with an empty datetime, for the user to fill in (`convert` refuses
    such a row, naming it). `bands` are the band names to carry: a CSV `bands` column (names
    joined by `;`), or for JSON a `bands` entry, which turns the list of rows into an object with
    `items`.
    """
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
    rows.extend({"uri": uri, "datetime": ""} for uri in undated)
    if path.suffix.lower() == ".json":
        document: Any = {"bands": list(bands), "items": rows} if bands else rows
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return
    fieldnames = ["uri", "datetime"]
    if bands:
        fieldnames.append("bands")
        for row in rows:
            row["bands"] = ";".join(bands)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer: Any = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
