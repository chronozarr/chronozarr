# Develop from source

This checkout targets Python and npm **0.5.0**. Use these instructions to run local source or qualify local artifacts. For a registry installation, start with the [quickstart](quickstart.md). The [release notes](release-0.5.0.md) describe changes and store compatibility.

The earlier [0.5.0rc1 preparation record](release-0.5.0rc1.md) describes the unpublished candidate that preceded this release. It is a historical record; the current guides all target 0.5.0.

## Install the command line tool

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then use a checkout of the release source. Replace `/absolute/path/to/chronozarr` with its local directory. The `v0.5.0` tag identifies the release source once publication completes.

```sh
cd /absolute/path/to/chronozarr
uv tool install --python 3.13 --force '.[geo,netcdf,cftime]'
chronozarr --version
```

The version must be `0.5.0` for these guides. The `geo` extra adds raster input/output and CRS conversion, `netcdf` adds the NetCDF reader and cftime, and `cftime` also supports climate calendars in Zarr inputs. To list S3 inputs, include `s3` in the same brackets. Include any publishing extras you use there too. Rerun the tool install from this checkout after changing its source or extras.

This tool has its own environment. It does not install the library into a notebook or another Python project.

## Python projects and notebooks

In the project where your script or notebook runs, add the source checkout by its absolute path:

```sh
uv add --editable '/absolute/path/to/chronozarr[geo,netcdf,cftime]'
uv run chronozarr --version
uv run python analyze.py
```

The editable dependency uses this checkout when you change its Python source. A notebook must use this project's environment as its kernel. An existing virtual environment can install the same local source with `uv pip install '/absolute/path/to/chronozarr[geo,netcdf,cftime]'`.

## Install local artifacts

After building or receiving the release artifacts, replace `/absolute/path/to/release-artifacts` with the folder that holds them. These commands install local files. Installing a local artifact does not publish it. Install either the source above or a wheel below in each environment.

For the standalone CLI:

```sh
uv tool install --python 3.13 --force '/absolute/path/to/release-artifacts/chronozarr-0.5.0-py3-none-any.whl[geo,netcdf,cftime]'
chronozarr --version
```

For the project where your Python analysis runs:

```sh
uv add '/absolute/path/to/release-artifacts/chronozarr-0.5.0-py3-none-any.whl[geo,netcdf,cftime]'
uv run chronozarr --version
```

Both Python version checks must report `0.5.0`. The source distribution is named `chronozarr-0.5.0.tar.gz`; the wheel avoids rebuilding it for installation.

In a JavaScript project, install the local npm tarball:

```sh
npm install /absolute/path/to/release-artifacts/chronozarr-0.5.0.tgz --min-release-age=7
npm ls chronozarr
npm exec -- chronozarr-viewer published --store ./store
```

`npm ls` must show version `0.5.0` for chronozarr. The last command copies the viewer from that installed package. It refuses to overwrite an existing `published/` folder. Copy your store into `published/store` before serving the folder.

## Build the viewer

The viewer served by chronozarr.org and a viewer installed from npm are separate builds from the Python package. Installing a Python checkout or wheel does not update either one. Use the local npm artifact above or build this checkout's [self-hosted viewer](viewer-distribution.md#build-from-source). Pass the copied viewer folder to `chronozarr preview --viewer-dir` or to `chronozarr.view(viewer_dir=...)`.

## Stores and versions

The 0.5.0 writer uses spec `0.3.0` for a CRS with an EPSG code and `0.4.0` otherwise. The 0.5.0 reader opens both. A reader from package 0.4.1 or earlier refuses a `0.4.0` store: upgrade the reader to 0.5.0 and use the matching viewer. Existing `0.3.0` stores need no conversion. The package version and store specification version are independent.
