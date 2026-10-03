"""Read a published or local store lazily and extract one pixel's complete time series."""

import argparse

import xarray as xr

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store")
    parser.add_argument("--row", type=int, default=0)
    parser.add_argument("--col", type=int, default=0)
    args = parser.parse_args()
    with xr.open_dataset(args.store, engine="chronozarr") as ds:
        print(ds)
        pixel = ds.isel(y=args.row, x=args.col)
        print("Physical values at the selected pixel (invalid samples are NaN):")
        print(pixel.to_dataframe().to_string())
