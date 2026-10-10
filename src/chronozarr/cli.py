"""Command line interface: `chronozarr encode | append | validate | info | doctor` and more."""

from __future__ import annotations

import glob
import inspect
import re
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import numpy as np
import xarray as xr

from chronozarr import schema
from chronozarr._convert_discover import is_discovery_source
from chronozarr._convert_source import UndeclaredNaNError
from chronozarr.append import append, is_store
from chronozarr.bands import (
    assign_roles,
    display_limits_apply,
    parse_roles,
    product_status,
    resolve_roles,
    set_band_roles,
)
from chronozarr.convert import (
    FIDELITY_HELP,
    RESAMPLING_METHODS,
    CogManifestSource,
    Entry,
    Plan,
    convert,
)
from chronozarr.decode import open_store
from chronozarr.doctor import DEFAULT_ORIGIN, diagnose, is_url
from chronozarr.encode import EncodeReport, encode
from chronozarr.export import export_cog, select_times
from chronozarr.schema import Band, SchemaError, validate
from chronozarr.stac import write_stac
from chronozarr.store import redact_url
from chronozarr.view import VIEWER_URL, preview, preview_command, viewer_url

_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{4})-?(\d{2})(?:-?(\d{2}))?(?!\d)")
_GLOB_CHARS = "*?["
_READ_ROWS = 512  # rows read at a time from a GeoTIFF; not the store chunk size


def _time_from_name(path: str) -> np.datetime64:
    match = _DATE_IN_NAME.search(Path(path).stem)
    if match is None:
        raise click.ClickException(
            f"cannot find a date (YYYY-MM, YYYY-MM-DD or YYYYMMDD) in file name '{path}'"
        )
    year, month, day = match.groups()
    try:
        return np.datetime64(f"{year}-{month}-{day or '01'}", "s")
    except ValueError as exc:
        raise click.ClickException(f"invalid date in file name '{path}': {exc}") from exc


@dataclass(frozen=True)
class _GeotiffStack:
    """The timesteps of a GeoTIFF glob and the band metadata and validity the files declare."""

    data: xr.DataArray  # (time, band, y, x) in the files' dtype; attrs hold the CRS and transform
    mask: xr.DataArray | None  # (time, y, x) uint8, 1 = valid; None when validity is a sentinel
    nodata: float | int | None
    bands: tuple[Band, ...]


def _read_geotiffs(pattern: str, crs: str | None) -> _GeotiffStack:
    """One GeoTIFF per timestep (date parsed from the file name), bands stacked.

    The files are read as `chronozarr convert` reads a manifest of COGs, so dtype, band names,
    scale, offset, units, nodata and masks follow its fidelity rules (`convert --help`). Every
    file must be on one grid; nothing is resampled. `crs` stands in for a file that has none.
    """
    paths = glob.glob(pattern)
    if not paths:
        raise click.ClickException(f"no files match '{pattern}'")
    entries = [Entry(path, time) for time, path in sorted((_time_from_name(p), p) for p in paths)]
    source = CogManifestSource(
        entries,
        None,
        target_crs=crs,
        target_transform=None,
        target_shape=None,
        resampling="nearest",  # only so that an off-grid file is reported below, not warped
        nodata="auto",
        chunk_size=_READ_ROWS,
    )
    if 0 in source.warped:
        raise click.ClickException(
            f"--crs {crs} is not the CRS of '{entries[0].uri}', and this command does not "
            "reproject. Use `chronozarr convert` with a manifest, or drop --crs"
        )
    if source.warped:
        raise click.ClickException(
            f"'{entries[min(source.warped)].uri}' differs from '{entries[0].uri}' in CRS, "
            "transform or shape, and this command does not resample. Use `chronozarr convert` "
            "with a manifest and --resampling"
        )

    info = source.info
    grid = info.grid
    data = np.empty((len(entries), info.n_band, grid.height, grid.width), dtype=info.dtype)
    valid = (
        np.empty((len(entries), grid.height, grid.width), dtype=np.uint8) if info.mask else None
    )
    for t in range(len(entries)):
        try:
            step = source.read(t)
        except UndeclaredNaNError as exc:
            # The error's own hint says `--nodata nan`, an option of `convert` only
            raise click.ClickException(
                f"{exc.where} holds {exc.count} NaN values, and the file declares no NaN nodata, "
                "so nothing marks them invalid. Declare NaN as the nodata of the files (for "
                "example `gdal_edit.py -a_nodata nan FILE`), or list them in a manifest and run "
                "`chronozarr convert --nodata nan MANIFEST OUT`, which marks NaN pixels invalid "
                "with a mask. `--nodata` is an option of `convert` only"
            ) from exc
        data[t] = step.data
        if valid is not None:
            assert step.valid is not None  # a source with a mask returns a plane per timestep
            valid[t] = step.valid
    return _GeotiffStack(
        data=xr.DataArray(
            data,
            dims=schema.DIMENSIONS,
            coords={"time": np.array(source.times), "band": list(info.band_names)},
            attrs={"crs": grid.crs, "transform": list(grid.transform)},
        ),
        mask=None if valid is None else xr.DataArray(valid, dims=schema.PLANE_DIMENSIONS),
        nodata=info.nodata,
        bands=info.bands,
    )


def _refuse_files_for_convert(input: str, out: Path) -> None:
    """Point a manifest, directory or S3 prefix at `convert`, the entry point for files on disk."""
    lowered = input.lower()
    if lowered.endswith((".csv", ".json")):
        kind = "a manifest"
    elif lowered.startswith("s3://") or (
        not any(ch in input for ch in _GLOB_CHARS) and is_discovery_source(input)
    ):
        kind = "a directory, S3 prefix or file of GeoTIFFs"
    else:
        return
    raise click.ClickException(
        f"'{input}' is {kind}, which `chronozarr encode` does not read. Use "
        f"`chronozarr convert {input} {out}`: it lists, checks and streams the files one "
        "timestep at a time"
    )


def _nodata_text(nodata: float | int | None) -> str:
    return "no nodata" if nodata is None else f"nodata {nodata}"


def _check_validity_matches_store(store: Path, stack: _GeotiffStack) -> None:
    """Fail when the files mark invalid pixels differently from `store`, before anything is copied.

    A store has one validity rule for every timestep (a nodata sentinel, a mask, or neither) and
    append can change neither the rule nor the presence of a mask (spec 8.3). The files' rule is
    what `CogManifestSource` made of what they declare. `append()` checks the mask itself, but only
    after copying the whole store, and in terms of arrays, not files.
    """
    attrs = open_store(store).attrs
    store_has_mask = attrs.mask_variable is not None
    files_have_mask = stack.mask is not None
    problems = []
    if store_has_mask and not files_have_mask:
        problems.append(
            "mask: the store has a mask and the files give none. A masked store needs a validity "
            "plane for every timestep, and a mask comes from an alpha band, an internal mask, or "
            "NaN declared as nodata (float data)"
        )
    elif files_have_mask and not store_has_mask:
        problems.append(
            "mask: the files give a mask and the store has none, which append cannot add. A mask "
            "comes from an alpha band, an internal mask or a nodata of NaN, so the new files must "
            "have none of these"
        )
    if stack.nodata != attrs.nodata:
        stored = _nodata_text(attrs.nodata)
        fix = (
            f"Rewrite the new files so their invalid pixels hold {attrs.nodata} and declare it "
            f"(for example `gdal_edit.py -a_nodata {attrs.nodata} FILE`)"
            if attrs.nodata is not None
            else "The store treats every pixel as valid, so remove the nodata from the new files "
            "(for example `gdal_edit.py -unsetnodata FILE`) if their pixels of that value are data"
        )
        problems.append(
            f"nodata: the files have {_nodata_text(stack.nodata)}, the store has {stored}. {fix}"
        )
    if problems:
        listed = "\n  - ".join(problems)
        raise click.ClickException(
            f"cannot append to {store}: the files do not mark invalid pixels the way the store "
            f"does, so nothing was written:\n  - {listed}\n"
            "To change the store's rule instead, encode it again from all its files."
        )


def _read_xarray(path: str, variable: str | None) -> xr.DataArray:
    source = Path(path)
    if not source.exists():
        raise click.ClickException(f"input '{path}' does not exist")
    dataset = (
        xr.open_dataset(source)
        if source.suffix == ".nc"
        else xr.open_zarr(source, chunks=None, consolidated=False)
    )
    if variable is None:
        names = list(dataset.data_vars)
        if len(names) != 1:
            raise click.ClickException(f"input has variables {names}; choose one with --variable")
        variable = str(names[0])
    if variable not in dataset.data_vars:
        raise click.ClickException(
            f"variable '{variable}' not in input: {list(dataset.data_vars)}"
        )
    da = dataset[variable]
    if set(da.dims) != set(schema.DIMENSIONS):
        raise click.ClickException(
            f"variable '{variable}' has dims {da.dims}; need {schema.DIMENSIONS}"
        )
    da = da.transpose(*schema.DIMENSIONS)
    for key in ("crs", "transform"):
        if key not in da.attrs and key in dataset.attrs:
            da.attrs[key] = dataset.attrs[key]
    return da


@contextmanager
def _command_errors() -> Iterator[None]:
    """Turn expected failures into one-line CLI errors, with an install hint for rasterio."""
    try:
        yield
    except ImportError as exc:
        if exc.name == "rasterio":
            raise click.ClickException(
                "this command needs rasterio: run `uv sync --extra geo` "
                "(or `pip install 'chronozarr[geo]'`)"
            ) from exc
        raise
    except (ValueError, OSError, SchemaError) as exc:
        notes = getattr(exc, "__notes__", [])
        raise click.ClickException("\n".join([str(exc), *notes])) from exc


_band_role_option = click.option(
    "--band-role",
    "band_roles",
    multiple=True,
    metavar="NAME=ROLE",
    help="Give band NAME the STAC common name ROLE (red, green, blue, nir, ...), so the "
    "viewer's products find it; 'none' removes one. Repeatable or comma-separated: "
    "--band-role B04=red,B08=nir. Only unambiguous names are detected without it.",
)


def _band_roles(texts: tuple[str, ...]) -> dict[str, str]:
    """Parsed `--band-role` values; a malformed one is a usage error."""
    try:
        return parse_roles(texts)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--band-role") from exc


def _encode_options(command: Any) -> Any:
    """Options shared by `encode` and `convert`; they map one-to-one onto `encode()` keywords."""
    options = [
        click.option(
            "--chunk-size",
            type=int,
            default=512,
            show_default=True,
            help="Spatial chunk edge in pixels (256 or 512 per the spec).",
        ),
        click.option(
            "--volatility", is_flag=True, help="Write the optional cell volatility metric."
        ),
        click.option(
            "--codec",
            type=click.Choice(["zstd", "blosc-zstd-shuffle"]),
            default="zstd",
            show_default=True,
            help="Chunk compression codec.",
        ),
        click.option(
            "--level",
            "compression_level",
            type=int,
            default=None,
            help="Compression level (default 5 for zstd, 1 for blosc).",
        ),
        click.option(
            "--shard/--no-shard",
            default=False,
            show_default=True,
            help="One shard object per (time shard, cell) instead of one chunk object per "
            "(timestep, cell). Fewer objects, but a CDN miss costs a whole shard and an append "
            "rewrites the trailing one.",
        ),
        click.option(
            "--shard-time",
            type=int,
            default=None,
            help="Timesteps per shard along time; needs --shard (default: all; may exceed the "
            "timesteps given).",
        ),
        click.option(
            "--lods", "n_lods", type=int, default=None, help="Pyramid levels including level 0."
        ),
        click.option(
            "--workers", type=int, default=None, help="Cells encoded concurrently (default 4)."
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _encode_kwargs(options: dict[str, Any]) -> dict[str, Any]:
    """`encode()` keywords from the parsed `_encode_options` values."""
    if options["shard_time"] is not None and not options["shard"]:
        raise click.UsageError("--shard-time needs --shard")
    return {
        "chunk_size": options["chunk_size"],
        "volatility": options["volatility"],
        "codec": options["codec"],
        "level": options["compression_level"],
        "shard": options["shard"],
        "shard_time": options["shard_time"],
        "n_lods": options["n_lods"],
        "workers": options["workers"],
    }


def _encode_summary(out: Path, report: EncodeReport) -> str:
    return (
        f"wrote {out}: {len(report.levels)} levels, {report.n_files} files, "
        f"{report.total_bytes / 1e6:.1f} MB, "
        f"{report.codec} level {report.level}"
    )


@click.group()
@click.version_option(package_name="chronozarr")
def main() -> None:
    """Zarr v3 stores for raster time series with true stored values."""


@main.command("encode")
@click.argument("input", metavar="INPUT")
@click.argument("out", type=click.Path(path_type=Path))
@_encode_options
@click.option("--crs", default=None, help="CRS such as EPSG:32631 (default: from the input).")
@click.option("--variable", default=None, help="Variable to encode from a Zarr/NetCDF input.")
@_band_role_option
def encode_command(
    input: str,
    out: Path,
    crs: str | None,
    variable: str | None,
    band_roles: tuple[str, ...],
    **options: Any,
) -> None:
    """Encode INPUT into a chronozarr store at OUT, holding the whole stack in memory.

    INPUT is a Zarr store or NetCDF file with dims (time, band, y, x), or a quoted glob of
    GeoTIFFs, one per timestep, with the date in the file name.

    For raster files on disk or in S3 use `chronozarr convert` instead: it takes directories,
    globs, S3 prefixes, manifests, Zarr and NetCDF, reads one timestep at a time and reports
    every incompatible file before writing.

    GeoTIFFs of uint8, uint16, int16 or float32 are read as `convert` reads COGs: band names,
    scale, offset, units, nodata and masks come from the files (see `convert --help`). The files
    must share one grid, and the whole stack is read into memory. To resample or to stream the
    files one timestep at a time, use `convert`.

    Band names are free. Run `chronozarr bands OUT` to see which viewer products the bands
    allow; --band-role B04=red,B08=nir sets the common names that select them.
    """
    roles = _band_roles(band_roles)
    file_options: dict[str, Any] = {}
    _refuse_files_for_convert(input, out)
    if any(ch in input for ch in _GLOB_CHARS):
        with _command_errors():
            stack = _read_geotiffs(input, crs)
        da = stack.data
        crs = None  # da.attrs holds the files' CRS, which --crs had to match
        file_options = {"bands": stack.bands, "nodata": stack.nodata, "mask": stack.mask}
    else:
        da = _read_xarray(input, variable)
    with _command_errors():
        if roles:
            named = file_options.get("bands") or [Band(str(n)) for n in da.coords["band"].values]
            file_options["bands"] = assign_roles(named, roles)
        report = encode(da, out, crs=crs, **file_options, **_encode_kwargs(options))
    click.echo(_encode_summary(out, report))
    click.echo(f"preview it: {preview_command(out)}")


@main.command("append")
@click.argument("store", type=click.Path(path_type=Path))
@click.argument("input", metavar="INPUT")
@click.option("--crs", default=None, help="CRS of the input, checked against the store.")
@click.option("--variable", default=None, help="Variable to append from a Zarr/NetCDF input.")
@click.option("--workers", type=int, default=None, help="Cells written concurrently (default 4).")
def append_command(
    store: Path, input: str, crs: str | None, variable: str | None, workers: int | None
) -> None:
    """Append the timesteps of INPUT to the end of the chronozarr store STORE, in place.

    INPUT is a chronozarr store (for example one month written by `convert`), a Zarr store or
    NetCDF file with dims (time, band, y, x), or a quoted glob of GeoTIFFs, one per timestep with
    the date in the file name. Its grid, bands, dtype, CRS and nodata must match STORE, and its
    times must come after the store's last one. GeoTIFFs must also mark invalid pixels as the
    store does: the nodata they declare (or none) is the store's, and they give a mask (an alpha
    band, an internal mask or a nodata of NaN) only if the store has one.

    Only the shards (or chunks, for an unsharded store) that gain a timestep are written, plus
    the metadata; every other object keeps its bytes. An unsharded store (the encoder default)
    writes only new chunk objects; a sharded one rewrites the shard that grows. Appending is not
    atomic: run it on a working copy and publish after `chronozarr validate`.
    """
    with _command_errors():
        if is_store(input):
            if crs is not None or variable is not None:
                raise click.UsageError("--crs and --variable do not apply to a chronozarr store")
            report = append(store, Path(input), workers=workers)
        elif any(ch in input for ch in _GLOB_CHARS):
            stack = _read_geotiffs(input, crs)
            _check_validity_matches_store(store, stack)
            report = append(store, stack.data, workers=workers, bands=stack.bands, mask=stack.mask)
        else:
            report = append(store, _read_xarray(input, variable), crs=crs, workers=workers)
    click.echo(
        f"appended {report.n_appended} timestep(s) to {store}: {report.n_time} in total, "
        f"wrote {report.objects_written} objects ({report.bytes_written / 1e6:.1f} MB) "
        f"in {report.seconds:.1f} s"
    )


@main.command("validate")
@click.argument("store")
def validate_command(store: str) -> None:
    """Check STORE against the chronozarr spec. Exit status 1 if it does not conform."""
    problems = validate(store)
    if problems:
        for problem in problems:
            click.echo(problem, err=True)
        click.echo(f"{store}: {len(problems)} problem(s)", err=True)
        sys.exit(1)
    click.echo(f"{store}: conforms to chronozarr {schema.SPEC_VERSION}")


@main.command("info")
@click.argument("store")
def info_command(store: str) -> None:
    """Summarise STORE: times, bands and pyramid levels."""
    with _command_errors():
        opened = open_store(store)
    attrs = opened.attrs
    click.echo(f"store:     {redact_url(store)}")
    click.echo(f"version:   chronozarr {attrs.spec_version}")
    click.echo(f"crs:       {attrs.crs}")
    click.echo(f"times:     {len(opened.times)} ({attrs.times[0]} .. {attrs.times[-1]})")
    click.echo(f"bands:     {', '.join(opened.bands)}")
    click.echo(f"nodata:    {attrs.nodata}")
    extras = [
        name
        for name, present in (
            ("mask", attrs.mask_variable),
            ("coverage", attrs.coverage_variable),
            ("provenance", attrs.provenance),
        )
        if present
    ]
    if extras:
        click.echo(f"extras:    {', '.join(extras)}")
    click.echo("levels:")
    for level in opened.levels:
        layout = f"shards {level.data.shards}" if level.data.shards else "unsharded"
        click.echo(
            f"  {level.index}: shape {level.shape}, {level.resolution:g} m/px, "
            f"grid {level.grid[0]}x{level.grid[1]}, chunks {level.data.chunks}, {layout}"
        )


_STATUS_LABEL = {"ok": "[ ok ]", "info": "[info]", "warn": "[warn]", "fail": "[FAIL]"}


@main.command("doctor")
@click.argument("target")
@click.option(
    "--origin",
    default=DEFAULT_ORIGIN,
    show_default=True,
    help="Origin header sent by the browser-style CORS checks (https URLs only).",
)
@click.option(
    "--full-read-limit-mb",
    type=float,
    default=16.0,
    show_default=True,
    help="Also compare a cell with a full-level read when one timestep is at most this big.",
)
def doctor_command(target: str, origin: str, full_read_limit_mb: float) -> None:
    """Diagnose TARGET, an https URL or a local store path.

    A URL is probed the way a browser would: root zarr.json, byte ranges, CORS, HEAD and caching
    headers. Both kinds then get the layout validated and one cell decoded per pyramid level,
    compared with a plain Zarr read and a full read. Exit status 1 if any check fails; warnings
    (advice) and info lines do not change the exit status.
    """
    checks = diagnose(target, origin=origin, full_read_limit_mb=full_read_limit_mb)
    width = max(len(c.name) for c in checks)
    click.echo(f"chronozarr doctor {redact_url(target)}")
    for check in checks:
        click.echo(f"{_STATUS_LABEL[check.status]} {check.name.ljust(width)}  {check.detail}")
        if check.hint and check.status in ("warn", "fail"):
            click.echo(f"       fix: {check.hint}")
    counts = {status: sum(c.status == status for c in checks) for status in _STATUS_LABEL}
    click.echo(
        f"{counts['ok']} ok, {counts['info']} info, {counts['warn']} warning(s), "
        f"{counts['fail']} failure(s)"
    )
    if counts["fail"]:
        sys.exit(1)


@main.command("preview")
@click.argument("store", type=click.Path(path_type=Path))
@click.option(
    "--port",
    type=click.IntRange(0, 65535),
    default=0,
    help="Port on 127.0.0.1 (default: a free one). An occupied port is an error.",
)
@click.option(
    "--viewer-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Self-hosted viewer folder (output of `chronozarr-viewer`), served by the same server. "
    "Needs no internet access.",
)
@click.option(
    "--viewer",
    default=None,
    help="URL of another viewer deployment (default: https://chronozarr.org/demo/, which needs "
    "internet access; the store is still read from this machine).",
)
@click.option(
    "--base-url",
    default=None,
    help="Absolute http(s) URL at which your browser reaches this server's root, when a proxy "
    "or port forward maps a different address to it. The server still listens on 127.0.0.1 only.",
)
@click.option("--no-open", is_flag=True, help="Print the URL without opening a browser.")
def preview_command_(
    store: Path,
    port: int,
    viewer_dir: Path | None,
    viewer: str | None,
    base_url: str | None,
    no_open: bool,
) -> None:
    """Serve the local store STORE and open it in the viewer. Ctrl-C stops the server.

    The server answers byte ranges with CORS on 127.0.0.1 and nothing else can reach it. The
    viewer comes from chronozarr.org unless --viewer-dir or --viewer is given.
    """
    with _command_errors():
        preview(
            store,
            port=port,
            viewer=viewer,
            viewer_dir=viewer_dir,
            base_url=base_url,
            open_browser=not no_open,
            echo=click.echo,
        )


@main.command("export-cog")
@click.argument("store")
@click.argument("out_dir", type=click.Path(path_type=Path))
@click.option("--level", type=int, default=0, show_default=True, help="Pyramid level to export.")
@click.option(
    "--times",
    "times",
    multiple=True,
    help="Timesteps: all (default), indices 0,5,-1, slices 0:12:3, dates 2024-03 or 2024-03-15, "
    "ranges 2020-01..2022-06. Repeatable.",
)
@click.option(
    "--physical",
    is_flag=True,
    help="Write float32 physical values (stored * scale + offset, NaN where invalid) instead of "
    "the stored values.",
)
def export_cog_command(
    store: str, out_dir: Path, level: int, times: tuple[str, ...], physical: bool
) -> None:
    """Export timesteps of STORE as true-value Cloud Optimized GeoTIFFs in OUT_DIR.

    STORE is a local path or https URL. One file per timestep, all bands, named
    L<level>_<date>.tif, readable by GDAL and QGIS without chronozarr. One timestep of the level
    is held in memory at a time, so use --level for very large stores.

    \b
    Validity and scaling are written as GDAL metadata:
      mask store    an internal per-dataset mask; the store's nodata is also set, unless a valid
                    pixel of that timestep holds it
      nodata store  the same nodata, judged per band, and no mask
      neither       no nodata and no mask: every pixel is valid
      scale, offset, units and band names go to the band metadata (not with --physical)
    """
    with _command_errors():
        opened = open_store(store)
        chosen = select_times(times, opened.attrs.times)
        paths = export_cog(opened, out_dir, level=level, times=chosen, physical=physical)
    total = sum(p.stat().st_size for p in paths)
    click.echo(f"wrote {len(paths)} COG(s), {total / 1e6:.1f} MB, to {out_dir}")


@main.command("stac")
@click.argument("store")
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(path_type=Path),
    help="Directory for collection.json and <id>/<id>.json.",
)
@click.option("--href", default=None, help="Public location of the store (default: see below).")
@click.option("--id", "stac_id", default=None, help="STAC id (default: <parent>-<name> of STORE).")
@click.option("--title", default=None, help="Human-readable title.")
@click.option("--description", default=None, help="Collection description.")
@click.option(
    "--license",
    "license_id",
    default="proprietary",
    show_default=True,
    help="SPDX identifier, 'various' or 'proprietary'.",
)
def stac_command(
    store: str,
    out_dir: Path,
    href: str | None,
    stac_id: str | None,
    title: str | None,
    description: str | None,
    license_id: str,
) -> None:
    """Write a static STAC Collection and Item for STORE (local path or https URL).

    The Item has the Zarr asset, spatial and temporal extent, band metadata, the datacube
    extension and the provenance recorded in the store. The asset href defaults to the URL for
    a remote store, or the relative path from the Item file to a local store; pass --href to
    publish the catalog next to a store served elsewhere.
    """
    with _command_errors():
        collection_path, item_path = write_stac(
            store,
            out_dir,
            href=href,
            id=stac_id,
            title=title,
            description=description,
            license=license_id,
        )
    click.echo(f"wrote {collection_path}")
    click.echo(f"wrote {item_path}")


def _parse_numbers(text: str | None, count: int, flag: str, kind: type) -> tuple | None:
    if text is None:
        return None
    try:
        values = tuple(kind(part) for part in text.split(","))
    except ValueError as exc:
        raise click.BadParameter(f"{flag} needs {count} comma-separated numbers: {exc}") from exc
    if len(values) != count:
        raise click.BadParameter(
            f"{flag} needs {count} comma-separated numbers, got {len(values)}"
        )
    return values


def _parse_shape(text: str | None) -> tuple[int, int] | None:
    numbers = _parse_numbers(text, 2, "--shape", int)
    return None if numbers is None else (int(numbers[0]), int(numbers[1]))


def _parse_nodata(text: str | None) -> float | int | str | None:
    if text is None:
        return "auto"
    if text.lower() == "none":
        return None
    try:
        number = float(text)
    except ValueError as exc:
        raise click.BadParameter(
            f"--nodata must be a number, 'none' or 'nan', got {text!r}"
        ) from exc
    return int(number) if number.is_integer() else number


@main.command("convert")
@click.argument("source")
@click.argument("out", type=click.Path(path_type=Path))
@_encode_options
@click.option("--variable", default=None, help="Variable to convert from a Zarr or NetCDF source.")
@click.option(
    "--dims",
    default=None,
    help="Dimension names when they are not time/y/x/band or common aliases: "
    "time=NAME,y=NAME,x=NAME[,band=NAME].",
)
@click.option(
    "--crs",
    default=None,
    help="EPSG:xxxxx. Manifest: the target CRS (default: the first source's) and the CRS of "
    "sources that declare none (a PNG with a world file). Zarr/NetCDF: the CRS of the data "
    "when the file does not declare one.",
)
@click.option(
    "--transform",
    default=None,
    help="Manifest only: target grid transform a,b,c,d,e,f (north-up). Needs --crs and --shape.",
)
@click.option("--shape", default=None, help="Manifest only: target grid height,width in pixels.")
@click.option(
    "--bounds",
    default=None,
    help="Manifest only: west,south,east,north of every frame in the units of --crs, for PNG "
    "frames with no world file or .aux.xml. Needs --crs; all frames must have one size.",
)
@click.option(
    "--resampling",
    type=click.Choice(RESAMPLING_METHODS),
    default=None,
    help="Warp COGs that are not on the target grid with this method (required when any are not).",
)
@click.option(
    "--nodata",
    default=None,
    help="Nodata value, 'none', or 'nan' (float data). Default: what the sources declare; none "
    "declared means no nodata, not 0. An explicit value replaces the declared one.",
)
@click.option(
    "--mask-var",
    default=None,
    help="Zarr/NetCDF only: a boolean or integer (time, y, x) variable whose nonzero values are "
    "valid. The store gets a mask.",
)
@click.option(
    "--work-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where timesteps are staged (default: OUT.convert-work beside OUT).",
)
@click.option("--resume", is_flag=True, help="Reuse timesteps staged by an interrupted run.")
@click.option(
    "--date-pattern",
    default=None,
    help="Directory, glob or S3 prefix only: where the date is in each file name, with %Y %m "
    "%d %H %M %S (for example 'ndvi_%Y%m%d'). Default: YYYYMMDD, YYYY-MM-DD or YYYY-MM, read "
    "only when a name holds exactly one date.",
)
@click.option(
    "--write-manifest",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory, glob or S3 prefix only: write the files and dates found as a manifest "
    "(.csv or .json) that convert reads back. Refuses to overwrite.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Check every file and print the dates, size and time estimates; writes no store. "
    "Reports all problems at once, grouped per file, with suggested fixes.",
)
@click.option(
    "--read-ahead",
    type=int,
    default=2,
    show_default=True,
    help="Timesteps read concurrently while staging; memory is about this many timesteps.",
)
@_band_role_option
def convert_command(
    source: str,
    out: Path,
    variable: str | None,
    dims: str | None,
    crs: str | None,
    transform: str | None,
    shape: str | None,
    bounds: str | None,
    resampling: str | None,
    nodata: str | None,
    mask_var: str | None,
    work_dir: Path | None,
    resume: bool,
    dry_run: bool,
    read_ahead: int,
    band_roles: tuple[str, ...],
    date_pattern: str | None,
    write_manifest: Path | None,
    **options: Any,
) -> None:
    """Convert existing raster files into a chronozarr store at OUT, one timestep at a time.

    This is the command for files on disk or in S3. SOURCE is one of:

    \b
      a directory, quoted glob or s3:// prefix of GeoTIFFs, each dated from its file name
      a manifest (.csv with columns uri,datetime[,bands] or .json) of COG or PNG frame URIs
      a Zarr store (path or URL) or a NetCDF file, the last two with --variable

    A directory or S3 prefix is not searched recursively; use a glob such as 'dir/**/*.tif'
    for subdirectories. S3 prefixes are listed with your AWS credentials and need
    `pip install 'chronozarr[s3]'`. A date is read from a name only when it holds exactly one
    (YYYYMMDD, YYYY-MM-DD or YYYY-MM); otherwise the file is reported and --date-pattern or a
    manifest says where the date is. --write-manifest saves what was found.

    Every file is checked before anything is read in bulk: dates, grid and CRS, bands, dtype,
    scale, offset, units and nodata. All problems are reported together, per file, with a
    suggested fix; nothing is resampled or rescaled unless you ask (--resampling). Each timestep
    is then read, staged under the work directory and encoded cell by cell. The size and time
    estimate is printed first; --dry-run stops there.

    For a Zarr or NetCDF array that fits in memory, `chronozarr encode` is the shorter route.
    --band-role NAME=ROLE sets a band's common name, e.g. for a manifest that names its bands
    (those get none) or sources whose band names the viewer does not know.
    """
    last_report = 0.0
    migration = False

    def show_plan(plan: Plan) -> None:
        nonlocal migration
        migration = plan.source.kind == "chronozarr v0.2"
        for line in plan.lines(read_ahead, list_files=dry_run):
            click.echo(line)

    def show_progress(done: int, total: int) -> None:
        nonlocal last_report
        now = time.monotonic()
        if done == total or now - last_report >= 2.0:
            last_report = now
            label = "verified level/timestep pairs" if migration else "staged timesteps"
            click.echo(f"{label}: {done}/{total}", err=True)

    with _command_errors():
        report = convert(
            source,
            out,
            variable=variable,
            dims=dims,
            crs=crs,
            transform=_parse_numbers(transform, 6, "--transform", float),
            shape=_parse_shape(shape),
            bounds=_parse_numbers(bounds, 4, "--bounds", float),
            resampling=resampling,
            nodata=_parse_nodata(nodata),
            mask_var=mask_var,
            band_roles=_band_roles(band_roles),
            work_dir=work_dir,
            resume=resume,
            dry_run=dry_run,
            read_ahead=read_ahead,
            date_pattern=date_pattern,
            write_manifest_to=write_manifest,
            on_plan=show_plan,
            progress=show_progress,
            **_encode_kwargs(options),
        )
    if write_manifest is not None:
        click.echo(f"wrote manifest {write_manifest}")
    if report.encode is None:
        click.echo("dry run: nothing was written")
        return
    click.echo(_encode_summary(out, report.encode))
    if migration:
        click.echo(
            f"verified every value in {len(report.encode.levels)} levels, "
            f"{report.plan.n_time} timesteps; total {report.total_s:.1f} s"
        )
        click.echo(f"preview it: {preview_command(out)}")
        return
    click.echo(
        f"read {report.n_staged} timesteps ({report.n_reused} reused) in {report.read_s:.1f} s, "
        f"encoded in {report.encode_s:.1f} s, total {report.total_s:.1f} s"
    )
    click.echo(f"preview it: {preview_command(out)}")


convert_command.help = f"{inspect.cleandoc(convert_command.help or '')}\n\n{FIDELITY_HELP}"


@main.command("publish")
@click.argument("store", type=click.Path(path_type=Path))
@click.option(
    "--destination",
    required=True,
    metavar="s3://BUCKET/PREFIX | gs://BUCKET/PREFIX | az://ACCOUNT/CONTAINER/PREFIX",
    help="Where to write: a storage location, not a browser URL. Use a fresh PREFIX per version.",
)
@click.option(
    "--public-url",
    default=None,
    metavar="HTTPS_URL",
    help="Address browsers read the store from (custom domain or CDN, ending at the store "
    "root). Required for R2 and other --endpoint-url hosts; for AWS S3, Google Cloud "
    "Storage and Azure Blob Storage it defaults to the storage's own HTTPS endpoint.",
)
@click.option(
    "--profile", default=None, help="Named AWS profile for s3:// (default: boto3's own chain)."
)
@click.option(
    "--endpoint-url",
    default=None,
    help="S3-compatible endpoint for s3://, e.g. https://<account id>.r2.cloudflarestorage.com "
    "for R2.",
)
@click.option(
    "--region", default=None, help="Region for s3:// (R2 endpoints use `auto` without this)."
)
@click.option(
    "--dry-run", is_flag=True, help="Print the plan and what is already stored; upload nothing."
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Replace objects whose content differs from the store. Never deletes. Objects are "
    "cached for a year, so prefer a fresh prefix.",
)
@click.option(
    "--update",
    is_flag=True,
    help="The prefix already holds an earlier version of STORE (published, then appended to): "
    "upload only the new and changed objects, new chunks first and the root zarr.json last, and "
    "keep the public URL and link. Refuses a prefix that is not an earlier state of STORE, and "
    "a sharded append that rewrites a trailing shard. Not atomic; rerun to resume.",
)
@click.option(
    "--apply-cors",
    is_flag=True,
    help="If the CORS check fails, add the viewer rule after the bucket's existing rules. "
    "Without this flag the bucket's CORS configuration is only read. Public access is never "
    "changed.",
)
@click.option("--workers", type=click.IntRange(min=1), default=16, show_default=True)
def publish_command(
    store: Path,
    destination: str,
    public_url: str | None,
    profile: str | None,
    endpoint_url: str | None,
    region: str | None,
    dry_run: bool,
    overwrite: bool,
    update: bool,
    apply_cors: bool,
    workers: int,
) -> None:
    """Upload STORE to your own static hosting and print a verified viewer link.

    Validates the store, uploads chunks before metadata (root zarr.json last) with cache
    headers, skips objects that are already stored (so a rerun resumes), runs the `doctor`
    checks against --public-url and prints a chronozarr.org/demo link only if they pass. The
    dataset stays on your host; its storage and delivery charges are yours. chronozarr.org
    serves the viewer, not the data. Credentials come from the provider's own chain (boto3,
    Google Application Default Credentials, Azure DefaultAzureCredential) and are never printed.

    With --update the prefix already holds an earlier version of STORE: only the objects that
    `chronozarr append` produced are uploaded, and the link stays the same. See docs/append.md for
    what readers see while it runs.
    """
    from chronozarr._publish_adapters import open_adapter
    from chronozarr.publish import (
        PublishError,
        failures,
        format_checks,
        inspect_destination,
        parse_destination,
        plan_store,
        publish,
    )
    from chronozarr.publish_update import plan_update, publish_update

    if update and overwrite:
        raise click.UsageError(
            "--update and --overwrite exclude each other: --update never replaces a chunk."
        )
    try:
        target = parse_destination(destination)
        adapter = open_adapter(target, profile=profile, endpoint_url=endpoint_url, region=region)
        plan = plan_store(store, target, adapter, public_url)
        if update:
            update_plan = plan_update(plan, adapter)
            click.echo(update_plan.summary(adapter))
            if dry_run:
                click.echo("dry run: nothing was uploaded")
                return
            result = publish_update(
                update_plan, adapter, apply_cors=apply_cors, workers=workers, log=click.echo
            )
        elif dry_run:
            state = inspect_destination(plan, adapter)
            click.echo(plan.summary(adapter, state.describe(overwrite)))
            if state.conflicts and not overwrite:
                raise click.ClickException(
                    "dry run: publishing would be refused, see prefix above"
                )
            click.echo("dry run: nothing was uploaded")
            return
        else:
            click.echo(plan.summary(adapter))
            result = publish(
                plan,
                adapter,
                overwrite=overwrite,
                apply_cors=apply_cors,
                workers=workers,
                log=click.echo,
            )
    except PublishError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"\nchronozarr doctor {plan.public_url}")
    click.echo(format_checks(result.checks))
    for note in result.notes:
        click.echo(f"\n{note}")
    if result.link is None:
        if any(not c.name.startswith("CORS") for c in failures(result.checks)):
            click.echo(f"\n{adapter.access_help()}")
        click.echo(
            "\nnot verified: the objects are stored but the hosted store failed the checks "
            "above, so no link is printed. Fix the listed items and run the same command again; "
            "stored objects are skipped.",
            err=True,
        )
        sys.exit(1)
    opening = (
        "verified. The viewer link is unchanged; open it (reload a page that is already open):"
        if update
        else "verified. Open the store in the viewer:"
    )
    click.echo(f"\n{opening}\n\n  {result.link}\n")
    click.echo(
        "The dataset stays on your host and its storage and delivery charges are yours; "
        "chronozarr.org supplies the viewer only."
    )


def _band_rows(bands: tuple[Band, ...]) -> list[tuple[str, ...]]:
    """Header and one row per band: name, common name, the roles it plays and why, scaling."""
    plays: dict[str, list[str]] = {band.name: [] for band in bands}
    for resolution in resolve_roles(bands).values():
        for name in resolution.bands:
            plays[name].append(f"{resolution.role} ({resolution.source})")
    rows = [("name", "common_name", "role", "scale", "offset", "units")]
    for band in bands:
        rows.append(
            (
                band.name,
                band.common_name or "-",
                ", ".join(plays[band.name]) or "-",
                f"{1.0 if band.scale is None else band.scale:g}",
                f"{0.0 if band.offset is None else band.offset:g}",
                band.units or "-",
            )
        )
    return rows


def _print_bands(bands: tuple[Band, ...]) -> None:
    rows = _band_rows(bands)
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    click.echo("bands:")
    for row in rows:
        click.echo(
            "  "
            + "  ".join(
                cell.ljust(width) for cell, width in zip(row, widths, strict=True)
            ).rstrip()
        )
    for resolution in resolve_roles(bands).values():
        if resolution.ambiguous:
            names = ", ".join(resolution.bands)
            click.echo(
                f"warning: {names} all answer to {resolution.role} (by {resolution.source}); the "
                f"viewer uses the first. Give {resolution.role} to one with --band-role "
                "NAME=ROLE and clear or reassign the others",
                err=True,
            )
    click.echo("products:")
    for status in product_status(bands):
        detail = "available" if status.available else f"needs {', '.join(status.missing)}"
        click.echo(f"  {status.name.ljust(12)}  {detail}")


@main.command("bands")
@click.argument("store")
@_band_role_option
@click.option("--dry-run", is_flag=True, help="Show the result of --band-role without writing it.")
def bands_command(store: str, band_roles: tuple[str, ...], dry_run: bool) -> None:
    """List the bands of STORE, the role each plays, and the viewer products they allow.

    The viewer picks the bands of True color, False color, NDVI, NDWI and Water by role: a
    band's common_name, else (when it has none) its name if that is red, green, blue or nir,
    else its Sentinel-2 name (B04, B03, B02, B08). Nothing else is guessed. For other names,
    --band-role NAME=ROLE sets the common_name in the local STORE, in place: only the root
    zarr.json (and consolidated metadata) changes, never data, scale, offset or units. A hosted
    copy needs its root zarr.json uploaded again and any CDN copy purged.
    """
    roles = _band_roles(band_roles)
    with _command_errors():
        opened = open_store(store)
        bands = opened.attrs.bands
        if roles:
            bands = set_band_roles(store, roles) if not dry_run else assign_roles(bands, roles)
    click.echo(f"store:     {store}")
    if roles:
        click.echo("dry run: nothing was written" if dry_run else "common names written")
    _print_bands(bands)


@main.command("link")
@click.argument("store_url")
@click.option("--product", default=None, help="Initial product id (see `chronozarr bands`).")
@click.option("--band", default=None, help="Band of the single-band product.")
@click.option(
    "--range",
    "limits",
    default=None,
    metavar="LOW,HIGH",
    help="Display limits of the single-band product, in physical units (the band's scale and "
    "offset applied). Needs --product band.",
)
@click.option("--time", "t", type=int, default=None, help="Initial timestep index.")
@click.option("--viewer", default=VIEWER_URL, show_default=True, help="Viewer URL.")
def link_command(
    store_url: str,
    product: str | None,
    band: str | None,
    limits: str | None,
    t: int | None,
    viewer: str,
) -> None:
    """Print a viewer URL that opens the hosted store STORE_URL at a chosen initial view.

    The product, band, display limits and timestep live in the URL, not in the store, so the
    store stays readable by every client and anyone with the URL sees the same first view. Each
    value is checked against the store (read over HTTP), because the viewer silently ignores one
    the store cannot honor. To set the bands a product uses, see `chronozarr bands`.
    """
    if not is_url(store_url):
        raise click.ClickException(
            f"{store_url} is not an http(s) URL. A link needs the hosted store; to look at a "
            "local one, use chronozarr.view() or chronozarr.player() in a notebook"
        )
    low_high = _parse_numbers(limits, 2, "--range", float)
    with _command_errors():
        opened = open_store(store_url)
        bands = opened.attrs.bands
        statuses = {s.id: s for s in product_status(bands)}
        if product is not None:
            if product not in statuses:
                raise click.BadParameter(
                    f"{product!r} is not a product; products are {', '.join(statuses)}",
                    param_hint="--product",
                )
            if not statuses[product].available:
                available = ", ".join(i for i, s in statuses.items() if s.available)
                raise click.BadParameter(
                    f"{product} needs {', '.join(statuses[product].missing)}, which no band "
                    f"is; available: {available}. See `chronozarr bands {store_url}`",
                    param_hint="--product",
                )
        if band is not None and band not in opened.bands:
            raise click.BadParameter(
                f"{band!r} is not a band; bands are {', '.join(opened.bands)}",
                param_hint="--band",
            )
        if t is not None and not 0 <= t < len(opened.times):
            raise click.BadParameter(
                f"{t} is outside 0..{len(opened.times) - 1}", param_hint="--time"
            )
        if low_high is not None:
            chosen = next(b for b in bands if b.name == (band or bands[0].name))
            if product != "band":
                raise click.UsageError("--range sets the single-band product: add --product band")
            if not display_limits_apply(opened.dtype.name, chosen):
                raise click.UsageError(
                    f"band {chosen.name} is shown as reflectance with fixed limits, so --range "
                    "would be ignored; choose a band whose values are not reflectance-like"
                )
        click.echo(viewer_url(store_url, viewer, t=t, product=product, band=band, range=low_high))
