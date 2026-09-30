"""Command line interface: `chronozarr encode | validate | info`."""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

import click
import numpy as np
import xarray as xr

from chronozarr import schema
from chronozarr.decode import open_store
from chronozarr.encode import encode
from chronozarr.schema import SchemaError, validate

_DATE_IN_NAME = re.compile(r"(?<!\d)(\d{4})-?(\d{2})(?:-?(\d{2}))?(?!\d)")
_GLOB_CHARS = "*?["


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


def _read_geotiffs(pattern: str) -> xr.DataArray:
    """One GeoTIFF per timestep (date parsed from the file name), bands stacked."""
    try:
        import rasterio
    except ImportError as exc:
        raise click.ClickException(
            "GeoTIFF input needs rasterio: run `uv sync --extra ingest`"
        ) from exc
    paths = glob.glob(pattern)
    if not paths:
        raise click.ClickException(f"no files match '{pattern}'")
    entries = sorted((_time_from_name(p), p) for p in paths)

    frames = []
    reference: tuple | None = None
    band_names: list[str] = []
    for _, path in entries:
        with rasterio.open(path) as src:
            signature = (src.crs, src.transform, src.shape, src.count)
            if reference is None:
                reference = signature
                if src.crs is None:
                    raise click.ClickException(f"'{path}' has no CRS")
                band_names = [d or str(i + 1) for i, d in enumerate(src.descriptions)]
            elif signature != reference:
                raise click.ClickException(
                    f"'{path}' differs from '{entries[0][1]}' in CRS, transform, shape or bands"
                )
            if src.dtypes[0] != "uint16":
                raise click.ClickException(
                    f"'{path}' is {src.dtypes[0]}; only uint16 is supported"
                )
            frames.append(src.read())
    assert reference is not None
    crs, affine, _, _ = reference
    return xr.DataArray(
        np.stack(frames),
        dims=schema.DIMENSIONS,
        coords={"time": np.array([t for t, _ in entries]), "band": band_names},
        attrs={"crs": crs.to_string(), "transform": list(affine)[:6]},
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
        variable = names[0]
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


@click.group()
@click.version_option(package_name="chronozarr")
def main() -> None:
    """Zarr v3 stores for raster time series with star-delta temporal encoding."""


@main.command("encode")
@click.argument("input", metavar="INPUT")
@click.argument("out", type=click.Path(path_type=Path))
@click.option("--chunk-size", default=512, show_default=True, help="Spatial chunk edge in pixels.")
@click.option("--anchor-interval", default=6, show_default=True, help="Timesteps between anchors.")
@click.option("--no-shard", is_flag=True, help="One chunk object per (timestep, cell).")
@click.option("--lods", "n_lods", type=int, default=None, help="Pyramid levels including level 0.")
@click.option("--crs", default=None, help="CRS such as EPSG:32631 (default: from the input).")
@click.option("--variable", default=None, help="Variable to encode from a Zarr/NetCDF input.")
@click.option("--workers", type=int, default=None, help="Worker threads (default: CPU count).")
def encode_command(
    input: str,
    out: Path,
    chunk_size: int,
    anchor_interval: int,
    no_shard: bool,
    n_lods: int | None,
    crs: str | None,
    variable: str | None,
    workers: int | None,
) -> None:
    """Encode INPUT into a chronozarr store at OUT.

    INPUT is a Zarr store or NetCDF file with dims (time, band, y, x), or a quoted glob of
    GeoTIFFs, one per timestep, with the date in the file name.
    """
    if any(ch in input for ch in _GLOB_CHARS):
        da = _read_geotiffs(input)
    else:
        da = _read_xarray(input, variable)
    try:
        report = encode(
            da,
            out,
            crs=crs,
            chunk_size=chunk_size,
            anchor_interval=anchor_interval,
            n_lods=n_lods,
            shard=not no_shard,
            workers=workers,
        )
    except (ValueError, FileExistsError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"wrote {out}: {len(report.levels)} levels, {report.n_files} files, "
        f"{report.total_bytes / 1e6:.1f} MB"
    )


@main.command("validate")
@click.argument("store")
def validate_command(store: str) -> None:
    """Check STORE against chronozarr v0.1. Exit status 1 if it does not conform."""
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
    """Summarise STORE: times, bands, temporal encoding and pyramid levels."""
    try:
        opened = open_store(store)
    except SchemaError as exc:
        raise click.ClickException(str(exc)) from exc
    attrs = opened.attrs
    temporal = attrs.temporal
    click.echo(f"store:     {store}")
    click.echo(f"version:   chronozarr {attrs.spec_version}")
    click.echo(f"crs:       {attrs.crs}")
    click.echo(f"times:     {len(opened.times)} ({attrs.times[0]} .. {attrs.times[-1]})")
    click.echo(f"bands:     {', '.join(opened.bands)}")
    click.echo(
        f"temporal:  {schema.ENCODING} every {temporal.anchor_interval}: "
        f"{len(temporal.anchor_indices)} anchors, {len(temporal.delta_reference)} deltas"
    )
    click.echo("levels:")
    for level in opened.levels:
        layout = f"shards {level.data.shards}" if level.data.shards else "unsharded"
        click.echo(
            f"  {level.index}: shape {level.shape}, {level.resolution:g} m/px, "
            f"grid {level.grid[0]}x{level.grid[1]}, chunks {level.data.chunks}, {layout}"
        )
