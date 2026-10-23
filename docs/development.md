# Use the local release candidate

The guides marked **Local release candidate — 0.5.0rc1, unpublished** describe the prepared candidate checkout. Its Python version is `0.5.0rc1`; its npm version is `0.5.0-rc.1`. These are two spellings of the same candidate. The candidate has not been published, tagged or deployed. See the [candidate release notes](release-0.5.0rc1.md).

The stable examples in this repository select Python and npm package 0.4.1; the [quickstart](quickstart.md) and [tagged documentation](https://github.com/chronozarr/chronozarr/tree/v0.4.1/docs) describe that release. Installing 0.4.1 does not provide the new converter options, calendar mapping, non-EPSG stores or the production viewer bundle.

## Install the command line tool

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then use the checkout prepared for this candidate. Replace `/absolute/path/to/chronozarr` with that local directory. Cloning remote main does not obtain the local candidate changes.

```sh
cd /absolute/path/to/chronozarr
uv tool install --python 3.13 --force '.[geo,netcdf,cftime]'
chronozarr --version
```

The version must be `0.5.0rc1` for the candidate guides. The `geo` extra adds raster input/output and CRS conversion, `netcdf` adds the NetCDF reader and cftime, and `cftime` also supports climate calendars in Zarr inputs. To list S3 inputs, include `s3` in the same brackets. Include any publishing extras you use there too. Rerun the tool install from this checkout after changing its source or extras.

This tool has its own environment. It does not install the library into a notebook or another Python project.

## Python projects and notebooks

In the project where your script or notebook runs, add the prepared candidate checkout by its absolute path:

```sh
uv add --editable '/absolute/path/to/chronozarr[geo,netcdf,cftime]'
uv run chronozarr --version
uv run python analyze.py
```

The editable dependency uses this checkout when you change its Python source. A notebook must use this project's environment as its kernel. An existing virtual environment can install the same local source with `uv pip install '/absolute/path/to/chronozarr[geo,netcdf,cftime]'`.

## Install local candidate artifacts

After building or receiving the candidate artifacts, replace `/absolute/path/to/rc-artifacts` with the folder that holds them. These commands install local files; they do not request an unpublished version from a registry. Install either the source above or a wheel below in each environment.

For the standalone CLI:

```sh
uv tool install --python 3.13 --force '/absolute/path/to/rc-artifacts/chronozarr-0.5.0rc1-py3-none-any.whl[geo,netcdf,cftime]'
chronozarr --version
```

For the project where your Python analysis runs:

```sh
uv add '/absolute/path/to/rc-artifacts/chronozarr-0.5.0rc1-py3-none-any.whl[geo,netcdf,cftime]'
uv run chronozarr --version
```

Both Python version checks must report `0.5.0rc1`. The source distribution is named `chronozarr-0.5.0rc1.tar.gz`; the wheel avoids rebuilding it for installation.

In a JavaScript project, install the local npm tarball:

```sh
npm install /absolute/path/to/rc-artifacts/chronozarr-0.5.0-rc.1.tgz --min-release-age=7
npm ls chronozarr
npm exec -- chronozarr-viewer published --store ./store
```

`npm ls` must show version `0.5.0-rc.1` for chronozarr. The last command copies the viewer from that installed candidate. It refuses to overwrite an existing `published/` folder. Copy your store into `published/store` before serving the folder. The artifact versions and local status do not imply that any upload or registry publication has occurred.

## Build the candidate viewer

The viewer served by chronozarr.org and a viewer installed from npm are separate builds from the Python package. Installing a candidate Python checkout or wheel does not update either one. For the features in the [candidate viewer guide](viewer.md), use the local npm artifact above or build this checkout's [self-hosted viewer](viewer-distribution.md). Pass the copied viewer folder to `chronozarr preview --viewer-dir` or to `chronozarr.view(viewer_dir=...)`.

## Stores and versions

The candidate writer uses spec `0.3.0` for a CRS with an EPSG code and `0.4.0` otherwise. The candidate reader opens both. A released 0.4.1 reader refuses a `0.4.0` store: use the candidate to read it, and use the viewer built from the candidate to view it. Existing `0.3.0` stores need no conversion. The package version and store specification version are independent.
